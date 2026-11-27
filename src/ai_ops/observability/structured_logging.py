"""结构化日志 — stdlib logging + JSON formatter（零新依赖）。

为什么不引 loguru / structlog：
- pyproject 已经够重（playwright + camoufox + Pillow），不想再加运行时依赖
- stdlib logging 已经够用，handler / formatter / extra 三件套覆盖 95% 场景
- 现有代码全部用 `logging.getLogger(__name__)` 模式——我们只在 root 层接管
  formatter，下游代码零改动

JSON Schema（一行一个 LogRecord）：
    {
        "timestamp": "2026-05-18T12:34:56.789Z",
        "level": "INFO",
        "logger": "ai_ops.scheduler.worker",
        "message": "job 42 success",
        "extra": {"job_id": 42, "account_id": 7}    // 可选
    }

如果 record 含 exc_info，会追加 "exception" 字段（traceback 字符串）。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

# LogRecord 的内置字段——extra 时要排除掉，避免污染 JSON
_RESERVED_LOG_RECORD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
})


class JsonFormatter(logging.Formatter):
    """把 LogRecord 序列化成单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        # 基础四字段
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # extra 字段：用户通过 logger.info("...", extra={"k": v}) 传入的会作为
        # LogRecord 属性挂上；排除掉 reserved 后即为业务字段
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in _RESERVED_LOG_RECORD_ATTRS and not k.startswith("_")
        }
        if extras:
            # 平铺到顶层（便于 ELK / Loki 字段查询）
            for k, v in extras.items():
                # 避免 extra key 与 reserved 冲突
                if k not in payload:
                    payload[k] = _safe_jsonable(v)

        # 异常信息：traceback
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def _safe_jsonable(value):
    """让 value 可 JSON 序列化——不可序列化的退化为 str。"""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def setup_logging(*, log_format: str = "text", log_level: str = "INFO") -> None:
    """配置 root logger handler + formatter。

    Args:
        log_format: "text"（人类可读，默认）或 "json"（结构化生产模式）
        log_level: "DEBUG" / "INFO" / "WARNING" / "ERROR"

    使用 ``logging.basicConfig(force=True)`` 接管 root——避免之前 uvicorn /
    其它库挂的 handler 与本 formatter 共存导致日志重复输出。
    """
    handler = logging.StreamHandler(sys.stdout)

    if log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        # text 模式：保留人类可读，但带上 logger 名称
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S%z",
            )
        )

    # force=True 清掉 root 上已有 handler，避免双重输出
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        handlers=[handler],
        force=True,
    )
    # httpx 在 INFO 级别记录每个请求行 ``HTTP Request: POST <完整 URL> ...``，
    # 而开放平台接口按官方约定把凭证放在查询串里（微信 access_token、快手
    # access_token/app_secret/refresh_token/upload_token）。默认 LOG_LEVEL=INFO，
    # 这些凭证因此会以明文写进 stdout / 日志采集。请求行没有不可替代的诊断
    # 价值，所以无论配置的级别多低，httpx logger 都硬性不低于 WARNING。
    _cap_httpx_logger()


def _cap_httpx_logger() -> None:
    """把 ``httpx`` logger 的级别钉在 WARNING：它的 INFO 记录含完整请求 URL。"""
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _restore_httpx_logger_for_test() -> None:
    """测试辅助：撤销 ``_cap_httpx_logger``，以免污染其它用例的 logger 级别。"""
    logging.getLogger("httpx").setLevel(logging.NOTSET)


def get_logger(name: str) -> logging.Logger:
    """返回 stdlib Logger（已通过 root handler 拿到结构化 formatter）。

    保持签名与 stdlib 一致——现有 `logging.getLogger(__name__)` 调用可以直接
    替换成 `get_logger(__name__)` 而无任何 API 差异。本 sprint 不强制改造
    现有 logger 调用，仅暴露 API，迁移留 follow-up。
    """
    return logging.getLogger(name)
