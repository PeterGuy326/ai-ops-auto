"""tests/test_initial_metrics.py — Task TD-Z3 · worker 接入 initial_metadata 落第一份 Metrics。

战场：`src/ai_ops/scheduler/worker.py` 新增的 `_persist_initial_metrics` helper +
`execute_job` 成功分支接入点。

背景：上轮 P7-C 已在 `publishers/toutiao.py:_do_publish` 把作品管理后台抓到的
view/comment/like 放进了 `PublishResult.raw_response["initial_metadata"]`——
但 worker 没接入，这份数据被丢弃。下游 `collect_metrics` 飞轮第一次跑要等 1h，
期间 dashboard / report 看不到任何数据 = publisher 的工作白做了。

测试契约（5 条）：
  1. 完整 initial_metadata → 新增一行 Metrics + 字段正确（含 UI 缩写解析）
  2. raw_response 不含 initial_metadata → 不落库（其他 publisher 路径）
  3. 全 0 数据 → 不落库（避免污染 dashboard，飞轮还会跑）
  4. 部分字段缺失 → 落库，缺失字段默认 0
  5. helper 抛异常 → publish 主流程不受影响（PublishResult 仍 success=True）

session 模板：沿用 P9 上轮收口的"单一信任源" SessionLocal.configure(bind=engine)，
不另起临时 sessionmaker——production-safe 约定（expire_on_commit=False）必须生效。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine

from ai_ops.core import db as db_mod
from ai_ops.core.enums import (
    AccountHealth,
    ArticleStatus,
    ContentType,
    JobStatus,
    Platform,
)
from ai_ops.core.models import Account, Article, Base, Metrics, PublishJob, Topic
from ai_ops.core.schemas import PublishResult
from ai_ops.scheduler import worker as worker_mod


# ---------------------------------------------------------------------------
# Fixture：rebind 生产 SessionLocal 到 in-memory engine（与 test_worker_integration 同套路）
# ---------------------------------------------------------------------------


@pytest.fixture
def production_session_in_memory():
    """把生产 `db.SessionLocal` rebind 到 in-memory engine。

    用 `SessionLocal.configure(bind=engine)` 而非新建临时 sessionmaker，
    确保 production kwargs（特别是 `expire_on_commit=False`) 生效——
    这是 P9 上轮收口的"单一信任源"约定。
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    original_bind = db_mod.SessionLocal.kw.get("bind")
    db_mod.SessionLocal.configure(bind=engine)
    try:
        yield db_mod.SessionLocal
    finally:
        db_mod.SessionLocal.configure(bind=original_bind)
        engine.dispose()


def _mk_publishable_chain(SessionLocal) -> int:
    """构造最小可发布链路 (topic → account → article → job)，返回 job_id。"""
    with SessionLocal() as s:
        topic = Topic(name="t_initmetrics", keywords=[], persona={}, target_platforms=[])
        s.add(topic)
        s.flush()
        acc = Account(
            platform=Platform.TOUTIAO,
            nickname="acc_initmetrics",
            health=AccountHealth.HEALTHY,
            encrypted_credential=b"",
        )
        s.add(acc)
        s.flush()
        article = Article(
            topic_id=topic.id,
            title="initial metrics 测试标题",
            body="正文，不含污点词",
            content_type=ContentType.IMAGE_TEXT,
            status=ArticleStatus.PUBLISHING,
            extra={},
        )
        s.add(article)
        s.flush()
        job = PublishJob(
            article_id=article.id,
            account_id=acc.id,
            platform=Platform.TOUTIAO,
            status=JobStatus.PENDING,
            publisher_kind="toutiao",
            attempts=0,
            max_attempts=3,
        )
        s.add(job)
        s.commit()
        return job.id


def _patch_worker_externals(monkeypatch):
    """统一桩掉 worker 外部依赖（凭证 / 限流 / 风控 / 健康 / notify / metrics）。

    与 test_worker_integration 同套路——只 patch 副作用 hook，
    不动 session 进出 / ORM access 路径，确保 _persist_initial_metrics 真实落库。
    """
    from ai_ops.accounts.manager import RateCheckResult

    monkeypatch.setattr(worker_mod, "get_credential", lambda s, aid: {"fake": "cred"})
    monkeypatch.setattr(
        worker_mod,
        "check_rate_limit",
        lambda s, aid: RateCheckResult(allowed=True, reason=""),
    )
    monkeypatch.setattr(worker_mod, "mark_published", lambda s, aid: None)
    monkeypatch.setattr(worker_mod, "is_paused", lambda acc: False)

    # 不让 schedule_after_publish 真起 APScheduler
    from ai_ops.scheduler import metrics as metrics_mod
    monkeypatch.setattr(metrics_mod, "schedule_after_publish", lambda jid: [])

    # notify 桩成 noop
    import ai_ops.notify as notify_mod
    monkeypatch.setattr(notify_mod, "publish_success", lambda snap: None)
    monkeypatch.setattr(notify_mod, "publish_failed", lambda snap: None)


def _run_execute_job_with_result(monkeypatch, job_id: int, result: PublishResult):
    """跑 execute_job 让 _try_publishers 返回指定 result——简化每个用例的样板。"""
    async def fake_try_publishers(platform, account_id, credential, content):
        return result
    monkeypatch.setattr(worker_mod, "_try_publishers", fake_try_publishers)
    return asyncio.run(worker_mod.execute_job(job_id))


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


class TestPersistInitialMetrics:
    """worker 接入 initial_metadata 落第一份 Metrics 的契约验证。"""

    def test_persists_initial_metrics_when_publisher_returns_data(
        self, production_session_in_memory, monkeypatch
    ):
        """raw_response 含完整 initial_metadata → Metrics 表新增一行 + 字段正确。

        关键：头条 UI 数字是字符串缩写（"1.2万" / "3.5k"），helper 必须走 _parse_count
        解析；raw 字段保留原始 dict 供观测。"""
        SessionLocal = production_session_in_memory
        job_id = _mk_publishable_chain(SessionLocal)
        _patch_worker_externals(monkeypatch)

        # 模拟 P7-C 改造后的 toutiao raw_response 结构
        initial_metadata = {
            "url": "https://www.toutiao.com/item/7350001234567890123/",
            "view_count": "1.2万",       # → 12000
            "comment_count": "234",      # → 234
            "like_count": "3.5k",        # → 3500
            "share_count": "12",         # → 12
            "publish_time": "刚刚",
        }
        result = PublishResult(
            success=True,
            platform_post_id="7350001234567890123",
            platform_url="https://www.toutiao.com/item/7350001234567890123/",
            raw_response={
                "final_url": "https://mp.toutiao.com/profile_v4/graphic/publish",
                "real_url": "https://www.toutiao.com/item/7350001234567890123/",
                "url_resolved_from_backend": True,
                "url_changed": True,
                "initial_metadata": initial_metadata,
            },
        )

        run_result = _run_execute_job_with_result(monkeypatch, job_id, result)
        assert run_result.success is True

        with SessionLocal() as s:
            metrics = s.query(Metrics).filter(Metrics.job_id == job_id).all()
            assert len(metrics) == 1, "应当落 1 行 initial Metrics"
            m = metrics[0]
            assert m.views == 12000, f"view_count '1.2万' → 12000，实际 {m.views}"
            assert m.likes == 3500, f"like_count '3.5k' → 3500，实际 {m.likes}"
            assert m.comments == 234
            assert m.shares == 12
            # raw 保留原始 dict 供观测
            assert m.raw["view_count"] == "1.2万"
            assert m.raw["url"].endswith("/item/7350001234567890123/")
            assert m.collected_at is not None

            # 副验证：job 仍然落 SUCCESS（接入不破坏主流程）
            job = s.get(PublishJob, job_id)
            assert job.status == JobStatus.SUCCESS

    def test_skips_when_initial_metadata_missing(
        self, production_session_in_memory, monkeypatch
    ):
        """raw_response 不含 initial_metadata → Metrics 表无新增。

        这是其他 publisher 路径（Zhihu / WechatMP / XHS 都不返 initial_metadata），
        helper 必须短路返回 None，绝不能因为缺 key 报错。"""
        SessionLocal = production_session_in_memory
        job_id = _mk_publishable_chain(SessionLocal)
        _patch_worker_externals(monkeypatch)

        result = PublishResult(
            success=True,
            platform_post_id="p_no_meta",
            platform_url="http://example.com/no_meta",
            raw_response={"final_url": "http://example.com/no_meta"},  # 没 initial_metadata
        )

        run_result = _run_execute_job_with_result(monkeypatch, job_id, result)
        assert run_result.success is True

        with SessionLocal() as s:
            metrics = s.query(Metrics).filter(Metrics.job_id == job_id).all()
            assert metrics == [], "无 initial_metadata 时不应落 Metrics 行"

    def test_skips_when_all_counts_zero(
        self, production_session_in_memory, monkeypatch
    ):
        """initial_metadata 全 0 → 不落库（避免污染 dashboard）。

        新发布常态：刚发出去还没人看到，view/like/comment 全 0；
        让飞轮 1h 后再落第一行非 0 数据，dashboard 才有真信号。"""
        SessionLocal = production_session_in_memory
        job_id = _mk_publishable_chain(SessionLocal)
        _patch_worker_externals(monkeypatch)

        result = PublishResult(
            success=True,
            platform_post_id="p_all_zero",
            platform_url="http://example.com/all_zero",
            raw_response={
                "final_url": "http://example.com/all_zero",
                "initial_metadata": {
                    "url": "http://example.com/all_zero",
                    "view_count": "0",
                    "comment_count": "0",
                    "like_count": "0",
                    "share_count": "",  # 空串也应被 _parse_count 兜底为 0
                    "publish_time": "刚刚",
                },
            },
        )

        run_result = _run_execute_job_with_result(monkeypatch, job_id, result)
        assert run_result.success is True

        with SessionLocal() as s:
            metrics = s.query(Metrics).filter(Metrics.job_id == job_id).all()
            assert metrics == [], "全 0 数据不应落库"

    def test_partial_fields_default_to_zero(
        self, production_session_in_memory, monkeypatch
    ):
        """只有 view_count 没 like → 落库（views 非 0），缺失字段默认 0。

        覆盖部分抓取成功场景：作品管理后台 selector 只命中一部分字段（头条 UI 改版）—— 
        只要至少一个 count 非 0，落库，让 dashboard 有信号；缺失字段统一兜底为 0。"""
        SessionLocal = production_session_in_memory
        job_id = _mk_publishable_chain(SessionLocal)
        _patch_worker_externals(monkeypatch)

        result = PublishResult(
            success=True,
            platform_post_id="p_partial",
            platform_url="http://example.com/partial",
            raw_response={
                "final_url": "http://example.com/partial",
                "initial_metadata": {
                    "url": "http://example.com/partial",
                    "view_count": "888",
                    # 没 like_count / comment_count / share_count
                    "publish_time": "刚刚",
                },
            },
        )

        run_result = _run_execute_job_with_result(monkeypatch, job_id, result)
        assert run_result.success is True

        with SessionLocal() as s:
            metrics = s.query(Metrics).filter(Metrics.job_id == job_id).all()
            assert len(metrics) == 1
            m = metrics[0]
            assert m.views == 888
            assert m.likes == 0
            assert m.comments == 0
            assert m.shares == 0
            # raw 保留全部入参（含 publish_time / url）
            assert m.raw["publish_time"] == "刚刚"

    def test_persist_exception_does_not_break_publish(
        self, production_session_in_memory, monkeypatch
    ):
        """mock _persist_initial_metrics 抛异常 → execute_job 仍 success=True。

        关键容错：哪怕 Metrics 写库挂了（外键约束 / 表损坏 / 任何 SQL 异常），
        publish 主流程绝不能跟着挂——job 仍要落 SUCCESS，notify 仍要发，飞轮仍要调度。
        helper 内已有 try/except + capture_exception，本测试通过外部强制抛异常验证拦截。"""
        SessionLocal = production_session_in_memory
        job_id = _mk_publishable_chain(SessionLocal)
        _patch_worker_externals(monkeypatch)

        # 强行让 helper 直接抛——模拟"helper 内 try 也救不回来"的极端情况
        # 实际上 worker 接入点没在 helper 外套 try，所以这里直接抛会让 execute_job 失败；
        # 这正好验证"helper 必须自己吞异常"的契约——如果未来重构去掉 helper 内 try，
        # 这条测试会立刻挂掉发出警报
        def boom(*a, **kw):
            raise RuntimeError("simulated metrics write boom")
        monkeypatch.setattr(worker_mod, "_persist_initial_metrics", boom)

        result = PublishResult(
            success=True,
            platform_post_id="p_boom",
            platform_url="http://example.com/boom",
            raw_response={
                "final_url": "http://example.com/boom",
                "initial_metadata": {
                    "url": "http://example.com/boom",
                    "view_count": "1234",
                    "like_count": "56",
                    "comment_count": "7",
                    "share_count": "8",
                },
            },
        )

        # 双层防御契约：
        #   1) helper 内部 try/except + capture_exception（业务异常自吞）
        #   2) worker 接入点再套一层 try/except（防 helper 被替换/重构破坏自吞契约）
        # 本用例 monkeypatch 强行让 helper 直接抛，验证外层 try 兜底——
        # publish 主流程绝不能被 Metrics 副作用拖挂。
        try:
            run_result = _run_execute_job_with_result(monkeypatch, job_id, result)
        except RuntimeError as e:
            pytest.fail(
                f"helper 异常被冒泡到 execute_job，破坏 publish 主流程: {e}. "
                f"必须在 worker 接入点加 try/except 兜底"
            )

        assert run_result.success is True
        # job 仍要落 SUCCESS
        with SessionLocal() as s:
            job = s.get(PublishJob, job_id)
            assert job.status == JobStatus.SUCCESS
            # Metrics 表应该没有新增行（helper 直接抛 = 没写成功）
            metrics = s.query(Metrics).filter(Metrics.job_id == job_id).all()
            assert metrics == [], "helper 抛异常时不应有 Metrics 行"
