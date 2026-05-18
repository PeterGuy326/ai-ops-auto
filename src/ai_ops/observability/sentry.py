"""Sentry 软依赖初始化。

底层逻辑：sentry-sdk 是个不小的运行时依赖（带 protobuf / urllib3 等传递依赖），
进 pyproject 强依赖 = 所有用户都被迫装。本项目大部分用户跑本地脚本不需要 Sentry，
所以走"软依赖"模式：

- 装了 sentry-sdk + 配了非空 dsn → 真启用
- 没装 / dsn 空 → 静默跳过，不报错

用户需要时自行 ``pip install sentry-sdk`` 即可，无需改本项目代码。

实现细节：用 ``importlib.util.find_spec`` 探测，而不是 module-level try-import——
后者在测试 ImportError 路径时不好 mock，前者每次调用都新鲜探测。
"""
from __future__ import annotations

import importlib
import importlib.util
import logging

logger = logging.getLogger(__name__)


def _sentry_sdk_available() -> bool:
    """探测 sentry-sdk 是否已安装。"""
    return importlib.util.find_spec("sentry_sdk") is not None


def init_sentry(*, dsn: str, environment: str = "dev", release: str = "") -> bool:
    """初始化 Sentry（如果可用）。

    Args:
        dsn: Sentry DSN。空字符串 = 不启用。
        environment: env 标签（dev / staging / prod）
        release: release 标签（推荐 "<project>@<version>"）

    Returns:
        True = 成功 init；False = 跳过（dsn 空 / sentry-sdk 未装 / init 失败）

    任何异常都被吞掉，不能因为 Sentry 自身炸了影响主进程启动。
    """
    if not dsn:
        # 空 dsn = 静默跳过（开发常态，不刷 warning）
        logger.debug("sentry: skipped (no DSN configured)")
        return False

    if not _sentry_sdk_available():
        # 配了 dsn 但没装 sdk—— warn 一下提示用户 pip install，不阻塞
        logger.warning(
            "sentry: DSN configured but sentry-sdk not installed; "
            "run `pip install sentry-sdk` to enable error tracking"
        )
        return False

    try:
        sentry_sdk = importlib.import_module("sentry_sdk")
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=release or None,
            # 默认采样：error 全收，trace 不收（避免 quota 爆炸）
            traces_sample_rate=0.0,
        )
        logger.info("sentry: initialized (env=%s release=%s)", environment, release or "n/a")
        return True
    except Exception as e:
        # 任何 init 失败（DSN 格式错 / 网络问题）都不能阻塞启动
        logger.warning("sentry: init failed (swallowed): %s", e)
        return False
