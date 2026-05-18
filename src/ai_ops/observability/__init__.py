"""Task G · 可观测性入口模块。

底层逻辑：发布失败 / 通知失败 / cron 异常都发生在异步路径（worker.execute_job /
scheduler 触发），stdout 日志没人会去翻；没 Sentry = 静默失败堆积，"事故发生
才知道"。本模块提供两件事：

1. **结构化日志**：JSON 格式（可选），含 timestamp / level / logger / message /
   extra 字段，对接 ELK / Datadog / Loki 等中心化收集。
2. **Sentry 软依赖**：装了 sentry-sdk + 配置 dsn 才启用；未装也不报错。

公共 API:
    init_observability() — lifespan startup 调一次，幂等
    get_logger(name) — 拿一个挂了结构化 handler 的 stdlib Logger
"""
from __future__ import annotations

import logging
from typing import Optional

from ..config import settings
from .sentry import init_sentry
from .structured_logging import get_logger, setup_logging

__all__ = ["init_observability", "get_logger"]

# 幂等标志：lifespan 多次启动（开发热重载）不重复初始化
_initialized = False


def init_observability(
    *,
    log_format: Optional[str] = None,
    log_level: Optional[str] = None,
    sentry_dsn: Optional[str] = None,
    sentry_environment: Optional[str] = None,
) -> None:
    """初始化结构化日志 + Sentry（如果可用）。

    参数都可选，缺省取 settings.*；显式传参主要给测试 / 多环境覆盖用。
    幂等：多次调用只生效第一次（避免热重载重复挂 handler 导致日志重复）。
    """
    global _initialized
    if _initialized:
        return

    fmt = log_format if log_format is not None else settings.log_format
    lvl = log_level if log_level is not None else settings.log_level
    dsn = sentry_dsn if sentry_dsn is not None else settings.sentry_dsn
    env = sentry_environment if sentry_environment is not None else settings.sentry_environment

    setup_logging(log_format=fmt, log_level=lvl)
    init_sentry(dsn=dsn, environment=env, release="ai-ops-auto@0.1.0")

    _initialized = True
    logging.getLogger(__name__).info(
        "observability initialized",
        extra={"log_format": fmt, "log_level": lvl, "sentry_enabled": bool(dsn)},
    )


def _reset_for_test() -> None:
    """测试辅助：重置 _initialized 让 setup_logging 可重复跑。"""
    global _initialized
    _initialized = False
