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


def capture_exception(exc: BaseException, **context) -> bool:
    """把异常上报到 Sentry（如可用），同时挂上 context 作 tags / extra。

    底层逻辑：上游 ``except Exception: pass`` 是观测黑洞——光打日志不接 Sentry =
    告警无法收敛，事故只能事后翻日志才发现。这层 helper 给 worker / health /
    notify 这种异步路径一个**统一的"接 Sentry"入口**，不强依赖 sentry-sdk。

    Args:
        exc: 要上报的异常对象（``except Exception as e`` 拿到的 ``e``）
        **context: 任意 kv 上下文。简单标量（str/int/bool/None）作为 ``set_tag``
            走索引便于聚合查询；复杂对象作为 ``set_extra`` 仅展示用。
            推荐至少传 ``scope="<module>.<场景>"`` 用于 Sentry 侧分组。

    Returns:
        True = 成功 capture 给 Sentry；False = 跳过（sdk 未装 / capture 内部异常）。

    任何异常都被吞——Sentry 自身炸了不能反过来影响主业务路径。
    """
    if not _sentry_sdk_available():
        # 软依赖：未装 sdk 不报错，让本地 / 测试环境直接跑通
        return False

    try:
        sentry_sdk = importlib.import_module("sentry_sdk")
        # push_scope：让本次 capture 携带的 context 不污染全局 scope
        with sentry_sdk.push_scope() as scope:
            for k, v in context.items():
                if isinstance(v, (str, int, bool)) or v is None:
                    # 标量进 tag：Sentry 支持按 tag 聚合 / 过滤
                    scope.set_tag(k, "" if v is None else str(v))
                else:
                    # 复杂对象进 extra：仅展示，不参与索引
                    scope.set_extra(k, v)
            sentry_sdk.capture_exception(exc)
        return True
    except Exception as e:
        # capture 自身炸了（DSN 网络问题 / sdk 内部 bug）也得吞，
        # 保持"观测层不能反咬业务"的铁律
        logger.debug("sentry.capture_exception swallowed: %s", e)
        return False
