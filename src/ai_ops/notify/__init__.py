"""通知模块对外 API — 事件级入口。

底层逻辑：把"发布层 5 个关键事件"统一收口在这里，业务侧（worker / health / cli）
只 import 一行 `from ai_ops.notify import publish_success` 即可，不需要知道
背后用飞书 / 钉钉 / dedup 怎么实现的——这是抽象的抓手。

事件清单对齐 docs/publishing-sop.md §六通知矩阵：
  1. publish_success — 单条发布成功 → 运营群
  2. publish_failed  — 单条发布失败 → 发布负责人
  3. account_expired — 登录态失效 → 账号负责人
  4. report_ready    — 日报/周报生成完成 → 运营群（A↔B 接口契约，签名锁死）
  5. content_taint   — 污点拦截（模板就绪，等 worker 前置 hook 接入；本 sprint
                       C 已实现 _pre_publish_check 但只 fail-fast，下个 sprint 由
                       worker 在 pre_err 分支调本函数。out of scope, follow-up）
  6. fanout_done     — 一轮 fanout 完成（同 5，等批处理 callback 接入。out of scope,
                       follow-up）

调用契约：
- 所有事件函数返回 None（fire-and-forget）
- 失败容错：双层防御——webhook 层吞网络异常；事件层 @safe 装饰器吞任何遗漏的异常。
  通知模块不能因为自身 bug 影响主业务，这是红线
- 接受 ORM 对象（PublishJob / Account）或纯 dict 都可——用 getattr 兜底
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Callable, Literal, Union

from ..observability import get_logger
from . import dedup, lark_cli, webhook

logger = get_logger(__name__)

# 类型别名：业务侧可以传 ORM 对象，也可以传 snapshot dict
_JobLike = Union[Any, dict]
_AccountLike = Union[Any, dict]


def _safe(fn: Callable) -> Callable:
    """事件函数兜底：任何异常都吞掉 + warning，绝不向上抛。

    底层逻辑：通知是辅助通道，不能因为通知模块自己挂了影响 worker.execute_job
    返回值——这是与 webhook 层独立的第二道防线（防御开发者在事件函数里写错
    或者依赖的 settings/dedup 出意外）。
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            logger.warning(
                "notify event swallowed exception",
                extra={"event": fn.__name__, "error": str(e)},
                exc_info=True,
            )
            return None

    return wrapper


def _g(obj: Any, key: str, default: Any = None) -> Any:
    """统一从 ORM 对象 or dict 取字段。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _platform_label(value: Any) -> str:
    """Platform 可能是 Enum 也可能是 str，统一渲染。"""
    if value is None:
        return "unknown"
    return getattr(value, "value", str(value))


def _send(text: str) -> bool:
    """双后端分发器 — 按 settings.notify_backend 决定走 lark-cli / webhook / 两者。

    底层逻辑：dev 走 lark-cli 零配置（本机 auth login 即用），prod 走 webhook 解耦人机依赖，
    迁移期 both 兜底（任一通道成功即视为 success）。各通道内部已吞异常返 bool，本函数
    只汇总；不抛异常给事件层（事件层还有 @_safe 第二道防线）。
    """
    from ..config import settings  # 延迟 import，避免循环

    backend = (settings.notify_backend or "both").lower()
    success = False

    if backend in ("lark_cli", "both"):
        chat_ids = lark_cli._parse_chat_ids(settings.lark_cli_chat_ids or "")
        if chat_ids:
            try:
                if lark_cli.send_via_lark_cli(text, chat_ids):
                    success = True
            except Exception as e:  # 兜底防御，lark_cli 内部已吞但加一层保险
                logger.warning(
                    "notify._send: lark_cli path raised",
                    extra={"event": "send_lark_cli_raised", "error": str(e)},
                )

    if backend in ("webhook", "both"):
        # webhook.send 在 url 未配置时已 silently skip 返 False
        try:
            if webhook.send(text):
                success = True
        except Exception as e:  # 兜底
            logger.warning(
                "notify._send: webhook path raised",
                extra={"event": "send_webhook_raised", "error": str(e)},
            )

    return success


# ============================================================================
# 事件 1: 单条发布成功
# ============================================================================
@_safe
def publish_success(job: _JobLike) -> None:
    """worker.execute_job 成功分支调用。

    消息模板对齐 publishing-sop §六：
        已发布：account_id={aid} 在 {platform} 发布《{title}》 {url}
    """
    job_id = _g(job, "id")
    account_id = _g(job, "account_id")
    platform = _platform_label(_g(job, "platform"))
    title = _g(job, "title", "（无标题）")
    url = _g(job, "platform_url", "") or ""

    ok, hint = dedup.should_send("publish_success", f"job:{job_id}")
    if not ok:
        return

    text = f"已发布：account_id={account_id} 在 {platform} 发布《{title}》 {url}".rstrip()
    if hint:
        text = f"[{hint}] {text}"
    _send(text)


# ============================================================================
# 事件 2: 单条发布失败
# ============================================================================
@_safe
def publish_failed(job: _JobLike) -> None:
    """worker.execute_job 失败分支调用。

    消息模板：job_id={jid} 发布失败：{error}
    去重 key 按 account_id 聚合 —— 同账号短时间多次失败（cookie 失效连环炸）
    聚合成首条 + 第 3 次，避免单账号把群刷屏。
    """
    job_id = _g(job, "id")
    account_id = _g(job, "account_id")
    error = _g(job, "error") or "unknown"
    ok, hint = dedup.should_send("publish_failed", f"account:{account_id}")
    if not ok:
        return

    text = f"job_id={job_id} 发布失败：{error}"
    if hint:
        text = f"[{hint}] {text}"
    _send(text)


# ============================================================================
# 事件 3: 账号登录态失效
# ============================================================================
@_safe
def account_expired(account: _AccountLike) -> None:
    """health.check_all_accounts 命中 EXPIRED/BANNED 时调用。

    消息模板：account_id={aid} {nickname}({platform}) 状态={health}，
              登录态失效，请 POST /accounts/{aid}/login 重登
    """
    account_id = _g(account, "id")
    nickname = _g(account, "nickname", "")
    platform = _platform_label(_g(account, "platform"))
    health = _g(account, "health", "")
    health_str = getattr(health, "value", str(health))

    ok, hint = dedup.should_send("account_expired", f"account:{account_id}")
    if not ok:
        return

    label = f"{nickname}({platform})" if nickname else f"{platform}"
    text = (
        f"account_id={account_id} {label} 状态={health_str}，"
        f"登录态失效，请 POST /accounts/{account_id}/login 重登"
    )
    if hint:
        text = f"[{hint}] {text}"
    _send(text)


# ============================================================================
# 事件 4: 日报 / 周报生成完成（与 A 的 notifier_stub 签名锁死）
# ============================================================================
@_safe
def report_ready(kind: Literal["daily", "weekly"], path: Union[str, Path]) -> None:
    """报告生成完成的通知钩子。

    A↔B 接口契约：参数名 (kind, path) 必须与 reports/notifier_stub.report_ready 一致。
    下个 sprint 由 A 把 cli_commands / cron 的 import 从 stub 切到本函数。
    """
    path_str = str(path)
    kind_label = "日报" if kind == "daily" else ("周报" if kind == "weekly" else kind)

    ok, hint = dedup.should_send("report_ready", f"kind:{kind}")
    if not ok:
        return

    text = f"{kind_label}已生成：{path_str}"
    if hint:
        text = f"[{hint}] {text}"
    _send(text)


# ============================================================================
# 事件 5 (模板就绪): 内容污点拦截 — out of scope, follow-up
# ============================================================================
@_safe
def content_taint(article_id: int, match: str) -> None:
    """污点拦截通知。

    publishing-sop §六规划事件，本 sprint 模板就绪但 **worker 前置 hook 未接入**
    （C 已实现 _pre_publish_check 但只 fail-fast 不通知；下个 sprint 由 worker
    在 pre_err 分支末尾调用本函数即可）。out of scope, follow-up。
    """
    ok, hint = dedup.should_send("content_taint", f"article:{article_id}")
    if not ok:
        return

    text = f"article_id={article_id} 正文含 {match}，发布已 abort，去编辑器修"
    if hint:
        text = f"[{hint}] {text}"
    _send(text)


# ============================================================================
# 事件 6 (模板就绪): 一轮 fanout 完成 — out of scope, follow-up
# ============================================================================
@_safe
def fanout_done(article_id: int, n_ok: int, n_fail: int) -> None:
    """fanout 完成通知。

    publishing-sop §六规划事件，本 sprint 模板就绪但 **批处理 callback 未接入**
    （scheduler 当前是 per-job 触发，没有"一批"概念；下个 sprint 加批 ID 后接入）。
    out of scope, follow-up。
    """
    ok, hint = dedup.should_send("fanout_done", f"article:{article_id}")
    if not ok:
        return

    text = f"article_id={article_id} fanout 完成：成功 {n_ok} / 失败 {n_fail}"
    if hint:
        text = f"[{hint}] {text}"
    _send(text)


__all__ = [
    "publish_success",
    "publish_failed",
    "account_expired",
    "report_ready",
    "content_taint",
    "fanout_done",
    "dedup",
    "webhook",
]
