"""发布任务执行器。

职责：拉取 PublishJob → 解密凭证 → 通过 registry 拿 Publisher → 调 publish →
落库结果（成功/失败/重试）→ 触发数据采集。

注意：发布器列表有优先级，fallback 自动切换。
"""
from __future__ import annotations

from datetime import datetime

from ..accounts.health_monitor import get_paused_until, is_paused
from ..accounts.manager import check_rate_limit, get_credential, mark_published, update_health
from ..core.db import session_scope
from ..core.dedup import is_too_similar
from ..core.enums import AccountHealth, ArticleStatus, ContentType, JobStatus, Platform
from ..core.models import Account, Article, Metrics, PublishJob
from ..core.schemas import PublishContent, PublishResult
from ..observability import get_logger
from ..observability.sentry import capture_exception
from ..publishers.registry import default_registry
# parse_count 已沉到 core/parsers（TD-Z3-debt 闭环, 2026 Q2）：
# 通用 UI 数字解析（"1.2万" / "3.5k" → int）是基础设施层，不该绑在 publisher 实现里。
# 上 sprint 用 `from ..publishers.toutiao import _parse_count` 是反向依赖（L5 调 L4），
# 本次改为从 core 正向 import，scheduler 和 publisher 双向解耦。
# 留 `_parse_count` 别名 → 模块内 _coerce_count 调用零改动。
from ..core.parsers import parse_count as _parse_count

logger = get_logger(__name__)

# 发布前置兜底污点词清单（命中即 fail-fast，防止 TODO / 未替换占位符 / 错版本号溜出）。
# 注：暂不进 config.py（Task B 在那条战线，避免合并冲突），下个 sprint 再迁移。
TAINT_PATTERNS: tuple[str, ...] = ("TODO", "未替换占位符", "过期版本号", "XXX")

# simhash 拦截阈值：与该账号 7d 内已发布 article.body 的 hamming 距离 < 此值即视为重复。
# 对齐 docs/anti-risk.md §63 设定的"相似度 > 0.85"，64 位 simhash 下约 8 bit。
SIMHASH_HAMMING_THRESHOLD = 8
SIMHASH_LOOKBACK_DAYS = 7


async def execute_job(job_id: int) -> PublishResult:
    """执行一个 PublishJob。"""
    with session_scope() as s:
        job: PublishJob | None = s.get(PublishJob, job_id)
        if job is None:
            return PublishResult(success=False, error=f"job {job_id} 不存在")

        article: Article | None = s.get(Article, job.article_id)
        if article is None:
            job.status = JobStatus.FAILED
            job.error = "article 缺失"
            return PublishResult(success=False, error=job.error)

        # 风控限流校验（养号期 + 间隔 + 单日上限）
        gate = check_rate_limit(s, job.account_id)
        if not gate.allowed:
            job.status = JobStatus.FAILED
            job.error = f"rate-limit: {gate.reason}"
            return PublishResult(success=False, error=job.error)

        # 风控降权暂停期检查（health_monitor 写入 account.profile["paused_until"]）
        account = s.get(Account, job.account_id)
        if account is not None and is_paused(account):
            until = get_paused_until(account)
            job.status = JobStatus.FAILED
            job.error = f"账号暂停中至 {until.isoformat() if until else 'unknown'}"
            return PublishResult(success=False, error=job.error)

        # 内容层前置兜底：TAINT 词 + simhash 查重。
        # 任何一个命中即 fail-fast，不再消耗下游的解密 / 浏览器开销。
        ok, pre_err = _pre_publish_check(s, job, article)
        if not ok:
            job.status = JobStatus.FAILED
            job.error = pre_err
            job.finished_at = datetime.utcnow()
            return PublishResult(success=False, error=pre_err)

        try:
            credential = get_credential(s, job.account_id)
        except ValueError as e:
            job.status = JobStatus.FAILED
            job.error = str(e)
            return PublishResult(success=False, error=str(e))

        platform = Platform(job.platform)
        content = _build_content(article)

        # 小红书图文：发布前对图片做反指纹处理（EXIF/微裁剪/微旋转/调色）
        # 仅对 XIAOHONGSHU + IMAGE_TEXT 执行，规避其它平台回归
        if (
            platform == Platform.XIAOHONGSHU
            and content.content_type == ContentType.IMAGE_TEXT
            and content.images
        ):
            try:
                from ..content.asset_processor import process_images
                content.images = process_images(content.images, job.account_id)
            except Exception as e:
                # 处理失败不阻断发布，沿用原图——但事故必须可观测，不能闷声
                logger.warning(
                    "worker.image_anti_fingerprint: swallowed",
                    extra={"job_id": job.id, "account_id": job.account_id, "error": str(e)},
                )
                capture_exception(
                    e,
                    scope="worker.image_anti_fingerprint",
                    job_id=job.id,
                    account_id=job.account_id,
                )

        job.status = JobStatus.RUNNING
        job.started_at = datetime.utcnow()
        job.attempts += 1
        s.flush()

    # 跳出 session 调外部工具，避免长事务
    result = await _try_publishers(platform, job.account_id, credential, content)

    with session_scope() as s:
        job = s.get(PublishJob, job_id)
        if job is None:
            return result
        job.finished_at = datetime.utcnow()
        if result.success:
            job.status = JobStatus.SUCCESS
            job.platform_post_id = result.platform_post_id
            job.platform_url = result.platform_url
            job.raw_response = result.raw_response
            mark_published(s, job.account_id)

            # 闭环最后一公里：把 publisher 主动塞进 raw_response 的第一份指标快照落库。
            # 不接入 = publisher 白做；接入后 dashboard / report 立刻有数（不用等 1h 飞轮）。
            # 同 session 内 add，依靠 session_scope commit。
            # 双层防御：helper 内已 try/except + capture；这里再套一层，防 helper
            # 被未来重构 / mock 替换破坏自吞契约后把 publish 主流程拖垮
            try:
                _persist_initial_metrics(
                    s,
                    job.id,
                    (result.raw_response or {}).get("initial_metadata") or {},
                )
            except Exception as e:
                logger.warning(
                    "worker.persist_initial_metrics_outer: swallowed",
                    extra={"job_id": job.id, "error": str(e)},
                )
                capture_exception(
                    e,
                    scope="worker.persist_initial_metrics_outer",
                    job_id=job.id,
                )

            article = s.get(Article, job.article_id)
            if article and article.status == ArticleStatus.PUBLISHING:
                article.status = ArticleStatus.PUBLISHED

            # 飞轮闭环：发布成功 → 调度 1h/24h/7d 数据采集
            try:
                from .metrics import schedule_after_publish
                schedule_after_publish(job.id)
            except Exception as e:
                # 采集失败不影响主流程——但必须留观测痕迹，否则飞轮长期断掉无人知
                logger.warning(
                    "worker.schedule_metrics: swallowed",
                    extra={"job_id": job.id, "error": str(e)},
                )
                capture_exception(e, scope="worker.schedule_metrics", job_id=job.id)
            # 通知模块快照（Task B）：在 session 内拼好数据，出块后再发——
            # 避免 notify 调用失败/慢回写影响 job 状态落库
            notify_snapshot = {
                "kind": "success",
                "id": job.id,
                "account_id": job.account_id,
                "platform": job.platform,
                "platform_url": job.platform_url,
                "title": (article.title if article else "（无标题）"),
            }
        else:
            job.error = result.error or "unknown"
            job.raw_response = result.raw_response
            if job.attempts < job.max_attempts:
                job.status = JobStatus.RETRYING
            else:
                job.status = JobStatus.DEAD
                # 失败联动：先降级到 DEGRADED；近 24h 内连续 3 次 DEAD → 升级到 BANNED
                _escalate_health_on_failure(s, job.account_id)
                # 自动重发钩子（publishing-sop §五 / §八"笔记发了发现内容错"自动通道）：
                # 默认关（AUTO_REPUBLISH_ON_DEAD=False）——避免 publisher 真挂时无限建 v2 → v3 → ...
                # 风暴。本钩子仅"建 v2 + 标 v1 superseded"，**不真触发 v2 执行**：
                # 让 scheduler.queue 按既有节奏拉起，复用风控 / 限流 / dedup 全套兜底。
                # 异常吞 + capture：自动重发是辅助通道，挂了不能拖累 job 状态本身的落库。
                if AUTO_REPUBLISH_ON_DEAD:
                    try:
                        v2 = republish_job(s, job.id, reason="auto_retry_exhausted")
                        logger.info(
                            "worker.auto_republish: created v2",
                            extra={"old_job_id": job.id, "new_job_id": v2.id},
                        )
                    except Exception as e:
                        logger.warning(
                            "worker.auto_republish: swallowed",
                            extra={"job_id": job.id, "error": str(e)},
                        )
                        capture_exception(e, scope="worker.auto_republish", job_id=job.id)
            # 通知模块快照（Task B）：失败也快照，session 外调 notify.publish_failed
            notify_snapshot = {
                "kind": "failed",
                "id": job.id,
                "account_id": job.account_id,
                "platform": job.platform,
                "error": job.error,
            }

    # 出 session 后异步通知——session_scope 已 commit，notify 异常不会回滚 job 状态
    try:
        from ..notify import publish_success, publish_failed
        if notify_snapshot["kind"] == "success":
            publish_success(notify_snapshot)
        else:
            publish_failed(notify_snapshot)
    except Exception as e:
        # 通知是辅助通道，任何异常都不能影响主业务返回值——
        # 但通知静默失败 = 运营群再也收不到消息，必须 capture 让 Sentry 兜底告警
        logger.warning(
            "worker.notify: swallowed",
            extra={
                "job_id": job_id,
                "kind": notify_snapshot.get("kind"),
                "error": str(e),
            },
        )
        capture_exception(
            e,
            scope="worker.notify",
            job_id=job_id,
            kind=notify_snapshot.get("kind"),
        )

    return result


async def _try_publishers(
    platform: Platform,
    account_id: int,
    credential: dict,
    content: PublishContent,
) -> PublishResult:
    """按优先级尝试该平台所有 Publisher，第一个成功即返回。"""
    publishers = default_registry.resolve(platform)
    if not publishers:
        return PublishResult(success=False, error=f"未注册 {platform} 的 Publisher")

    last: PublishResult | None = None
    for pub in publishers:
        try:
            result = await pub.publish(account_id, credential, content)
        except NotImplementedError as e:
            result = PublishResult(success=False, error=f"{pub.kind} 未实现: {e}")
        except Exception as e:
            result = PublishResult(success=False, error=f"{pub.kind} 异常: {e}")
        if result.success:
            return result
        last = result
    return last or PublishResult(success=False, error="所有 Publisher 都失败")


def _escalate_health_on_failure(session, account_id: int) -> None:
    """失败联动健康降级：DEAD 默认降到 DEGRADED；24h 内连续 3 次 DEAD 升级到 BANNED。"""
    from datetime import datetime, timedelta
    from sqlalchemy import func, select

    window_start = datetime.utcnow() - timedelta(hours=24)
    recent_dead = session.scalar(
        select(func.count(PublishJob.id))
        .where(PublishJob.account_id == account_id)
        .where(PublishJob.status == JobStatus.DEAD)
        .where(PublishJob.finished_at >= window_start)
    ) or 0

    if recent_dead >= 3:
        update_health(session, account_id, AccountHealth.BANNED)
    else:
        update_health(session, account_id, AccountHealth.DEGRADED)


def _build_content(article: Article) -> PublishContent:
    images = [a.local_path for a in article.assets if a.asset_type == "image"]
    videos = [a.local_path for a in article.assets if a.asset_type == "video"]
    return PublishContent(
        title=article.title,
        body=article.body,
        content_type=article.content_type,
        images=images,
        videos=videos,
        tags=article.extra.get("tags", []) if article.extra else [],
        extra=article.extra or {},
    )


def _pre_publish_check(
    session,
    job: PublishJob,
    article: Article,
    *,
    similarity_checker=None,
) -> tuple[bool, str | None]:
    """发布前置内容兜底：TAINT 词 grep + simhash 查重。

    Args:
        session: SQLAlchemy session（worker 已持有；这里不开新连接）。当前 TAINT 检查
            只读 article.body，simhash 通过 similarity_checker 走（默认调
            ``core.dedup.is_too_similar``，内部自带 session_scope）。
        job: PublishJob，提供 account_id 作为 simhash 查重的 scope key。
        article: Article，提供 body 作为待检测文本。
        similarity_checker: 可注入的相似度检测函数（签名同 is_too_similar），
            主要给单测注入 mock 用；生产路径默认 = is_too_similar。

    Returns:
        (ok, error_message)：ok=False 时 error_message 给 worker 写入 job.error。

    职责单一：只判断"能不能发"，不动 job / article 任何字段——状态机由调用方处理。
    """
    body = (article.body or "")

    # TAINT 词 grep：命中第一个即返回，避免拼接所有命中浪费日志位
    for pattern in TAINT_PATTERNS:
        if pattern in body:
            return False, f"污点拦截: 正文含 {pattern}"

    # simhash 查重：空 body 直接放行（不报错，让下游自己决定要不要发空内容）
    if not body.strip():
        return True, None

    checker = similarity_checker if similarity_checker is not None else is_too_similar
    try:
        too_similar = checker(
            text=body,
            account_id=job.account_id,
            days=SIMHASH_LOOKBACK_DAYS,
            threshold=SIMHASH_HAMMING_THRESHOLD,
        )
    except Exception as e:
        # 查重失败不阻断主流程：宁可发出去也不要因为 dedup bug 卡住运营节奏
        # （生产路径用 is_too_similar 内部已 try 兜底；这里再加一层防御）
        # 静默放行 + 观测兜底——dedup 长期失效 = 重复内容溢出 + 平台限流风险升级
        logger.warning(
            "worker.simhash_check: swallowed",
            extra={"job_id": job.id, "account_id": job.account_id, "error": str(e)},
        )
        capture_exception(
            e,
            scope="worker.simhash_check",
            job_id=job.id,
            account_id=job.account_id,
        )
        return True, None
    if too_similar:
        return False, (
            f"simhash 重复: 与账号 {job.account_id} 近 "
            f"{SIMHASH_LOOKBACK_DAYS}d 已发布内容相似度过高"
            f"（hamming < {SIMHASH_HAMMING_THRESHOLD}）"
        )

    return True, None


# ---------------------------------------------------------------------------
# initial_metadata → Metrics 落库（TD-Z3, 2026 Q2）
# ---------------------------------------------------------------------------


def _coerce_count(value) -> int:
    """把 initial_metadata 里的 count 字段统一收敛为 int。

    宽容输入：
      - int → 直接返回（其他 publisher 后续可能直接返 int）
      - str → 走 _parse_count（兼容 "1.2万" / "3.5k" 等 UI 缩写，头条当前路径）
      - None / 其他 → 0
    """
    if isinstance(value, bool):
        # bool 是 int 子类，必须先排除——不然 True/False 会被当 1/0 静默吃掉
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return _parse_count(value)
    return 0


def _persist_initial_metrics(
    session,
    job_id: int,
    initial_metadata: dict,
) -> "Metrics | None":
    """publish 成功后落第一份 Metrics 快照。

    数据流闭环：publisher._do_publish 抓到的 view/comment/like 已塞进
    raw_response["initial_metadata"]——本函数把它真正写到 Metrics 表，
    省下游 collect_metrics 飞轮 1h 后才出第一份数据的等待窗口。

    Args:
        session: SQLAlchemy session（worker 已持有；这里不开新连接、不 commit，
            commit 由 worker 外层 session_scope 统一管）。
        job_id: PublishJob.id，作为 Metrics.job_id 外键。
        initial_metadata: publisher 塞进 raw_response 的 dict，常见字段：
            {url, view_count, comment_count, like_count, share_count, publish_time}
            字段值可能是 int 或 UI 字符串（如 "1.2万"），统一走 _coerce_count 收敛。

    Returns:
        Metrics 实例（已 add 进 session），或 None（数据为空 / 全 0 / 异常）。

    短路策略：
      - initial_metadata 为空 dict → 返回 None（其它 publisher 不返 metadata 即此路径）
      - 所有计数都解析为 0 → 返回 None（避免污染数据；下游飞轮 1h 后还会跑）

    容错策略：
      - 任何异常 → logger.warning + capture_exception + 返回 None
      - publish 主流程不受影响（哪怕 Metrics 表写挂了，job 已落 SUCCESS）
    """
    if not initial_metadata:
        return None

    try:
        views = _coerce_count(initial_metadata.get("view_count"))
        likes = _coerce_count(initial_metadata.get("like_count"))
        comments = _coerce_count(initial_metadata.get("comment_count"))
        shares = _coerce_count(initial_metadata.get("share_count"))

        if views == 0 and likes == 0 and comments == 0 and shares == 0:
            # 全 0 → 不落库。这是新发布常态（刚发出去还没人看到），让飞轮 1h 后再落
            # 第一行非 0 数据；避免 dashboard 看到"发了 = 全 0"的歧义信号
            return None

        metric = Metrics(
            job_id=job_id,
            views=views,
            likes=likes,
            comments=comments,
            shares=shares,
            raw=dict(initial_metadata),  # 浅拷贝隔离，避免后续修改 raw_response 时联动
        )
        session.add(metric)
        session.flush()
        return metric
    except Exception as e:
        # 落库失败不影响 publish 主流程——但飞轮永远 1h 后才有第一份数据 = 仪表盘
        # 体感差。必须 capture 让 Sentry 兜底告警
        logger.warning(
            "worker.persist_initial_metrics: swallowed",
            extra={"job_id": job_id, "error": str(e)},
        )
        capture_exception(
            e,
            scope="worker.persist_initial_metrics",
            job_id=job_id,
        )
        return None


# ---------------------------------------------------------------------------
# 重发覆盖追踪 helper（publishing-sop §五 / §九 #7）
# ---------------------------------------------------------------------------


def _mark_job_superseded(session, old_job_id: int, new_job_id: int) -> bool:
    """把旧 PublishJob 标记为被新 job 覆盖。

    使用场景（本 Task 暂不创建调用方，仅暴露字段 + helper 给后续重发流程用）：
      - worker / 运营手动创建新 job 替代旧 job（旧 job 内容错 / 失败需重发）
      - 调用方先创建新 job，再调本 helper 把旧 job.superseded_by_job_id 指向新 job
      - 后台 UI / 周报 / 数据分析据此追踪"旧 job 被谁覆盖"，运营复盘有据

    Args:
        session: SQLAlchemy session（调用方负责 commit；本函数不开新连接、不 commit）
        old_job_id: 被覆盖的旧 PublishJob.id
        new_job_id: 覆盖它的新 PublishJob.id

    Returns:
        True  = 旧 job 存在且字段已 set
        False = 旧 job 不存在（调用方应日志告警）

    防御：
        - old == new → 拒绝（自指 = 数据污染）。返回 False 不抛，让上游决定降级
        - 不校验 new_job_id 是否真存在（FK 在 DB 侧兜底；helper 保持薄）
    """
    if old_job_id == new_job_id:
        # 自指防御：旧 job 指向自己 = 语义错乱。不抛异常以免阻塞主流程，
        # 但返回 False 让调用方有机会观测到。
        logger.warning(
            "worker._mark_job_superseded: refused self-reference",
            extra={"old_job_id": old_job_id, "new_job_id": new_job_id},
        )
        return False

    old = session.get(PublishJob, old_job_id)
    if old is None:
        logger.warning(
            "worker._mark_job_superseded: old job not found",
            extra={"old_job_id": old_job_id, "new_job_id": new_job_id},
        )
        return False

    old.superseded_by_job_id = new_job_id
    session.flush()
    return True


# ---------------------------------------------------------------------------
# 重发覆盖主流程（publishing-sop §五"重发覆盖语义" / §八风险表）
# ---------------------------------------------------------------------------

# 自动重发开关：默认关。打开 = execute_job 把 attempts >= max_attempts 的 job 标 DEAD 后
# 自动建 v2 PublishJob（v1.superseded_by_job_id 指向 v2）。
#
# 默认关的原因：
#   - publisher 真坏掉时（cookies 失效 / 平台改版 / 网络断），重试只会无限建 v2 → v3 → ...
#     形成风暴，反而把账号刷成 BANNED；
#   - 自动重发应该被外层"健康度评估 + 人工 review"门控；
#   - 运营拿到失败告警后手动调 POST /jobs/{id}/republish 才是当前推荐路径。
#
# 何时打开：accounts.health_monitor 接入"按账号自动判断是否值得重发"之后（follow-up）。
AUTO_REPUBLISH_ON_DEAD = False

# 允许重发的旧 job 状态白名单：只有真"跑挂了"的 job 才允许覆盖重发。
# - SUCCESS：已发布成功，重发 = 重复发，应走平台手动删 + 重新建 article 路径
# - PENDING / RUNNING / RETRYING：job 还在进行中，重发会形成竞态（多个 worker 抢同一 article）
# 只放行 FAILED / DEAD —— 前者是单轮失败、后者是耗尽重试。
_REPUBLISHABLE_STATUSES = (JobStatus.FAILED, JobStatus.DEAD)


def republish_job(session, old_job_id: int, *, reason: str = "manual") -> PublishJob:
    """基于失败的旧 PublishJob 创建 v2，并把旧 job 标记为 superseded。

    主流程入口（publishing-sop §五"重发覆盖语义"的物理载体）：
      - 人工触发：POST /jobs/{id}/republish（运营 UI 按钮）→ reason="manual"
      - 自动触发：execute_job 在 max_attempts 耗尽时调（AUTO_REPUBLISH_ON_DEAD=True 时）
        → reason="auto_retry_exhausted"

    本函数只"建 v2 + 标 v1 superseded"，**不真触发 v2 执行**——让 scheduler 拉起，
    复用现有风控 / 限流 / dedup / 健康度评估全套兜底，避免重发流程绕开主路径。

    Args:
        session: SQLAlchemy session（**不在函数内 commit**，commit 由调用方的 session_scope
            / API 的 get_session 统一管；保持 helper 薄）
        old_job_id: 被覆盖的旧 PublishJob.id（必须存在且 status ∈ {FAILED, DEAD}）
        reason: 重发原因，写入 v2.raw_response["republish_reason"]，用于运营复盘。
            约定值："manual" | "auto_retry_exhausted"；其它字符串也接受（向前兼容）

    Returns:
        新建的 v2 PublishJob 实例（已 add 进 session 并 flush，id 已分配）

    Raises:
        ValueError: 旧 job 不存在 / 状态不在白名单。调用方负责转译为 HTTP 400 等。

    数据契约（v2 vs v1）：
      - 复用：article_id / account_id / platform / publisher_kind / max_attempts
      - 重置：status=PENDING, attempts=0, started_at/finished_at/platform_*/error=None
      - 新写：raw_response = {"republish_reason": reason, "republished_from": old_id}
      - 关联：v1.superseded_by_job_id = v2.id（via _mark_job_superseded）
    """
    old = session.get(PublishJob, old_job_id)
    if old is None:
        raise ValueError(f"job {old_job_id} not found")

    if old.status not in _REPUBLISHABLE_STATUSES:
        raise ValueError(
            f"can only republish FAILED/DEAD jobs, got {old.status}"
        )

    new_job = PublishJob(
        article_id=old.article_id,
        account_id=old.account_id,
        platform=old.platform,
        publisher_kind=old.publisher_kind,
        status=JobStatus.PENDING,
        attempts=0,
        max_attempts=old.max_attempts,
        raw_response={
            "republish_reason": reason,
            "republished_from": old.id,
        },
    )
    session.add(new_job)
    session.flush()  # 拿 new_job.id

    # 标 v1 superseded（helper 自带"老 job 不存在则降级"，但此处老 job 100% 存在）
    _mark_job_superseded(session, old.id, new_job.id)
    return new_job

