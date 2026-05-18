"""通知模块 · lark-cli OpenAPI 后端。

底层逻辑：本机已 lark-cli auth login（user 身份，scope 含 im:message / im:chat），
可直接 subprocess 调 `lark-cli im +messages-send` 发到任意已加入群——dev 零配置即用，
不需要 webhook URL / 签名 / 在 UI 创建机器人。生产环境 cli 不便部署 → 走 webhook 解耦。

容错原则（与 webhook.send 同模式）：
  - lark-cli 未安装（shutil.which 返 None）→ 静默返 False（软依赖）
  - subprocess 非零退出 / TimeoutExpired / JSONDecodeError → logger.warning + 返 False
  - 任何意外异常 → 兜底吞掉返 False，绝不抛给 notify 事件层

子进程安全：
  - 必须用 argv list（subprocess argv 形式），不用 shell=True——文本含特殊字符不能被 shell 解释
  - text=True + encoding="utf-8" 显式指定，避免中文消息编码异常
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Iterable

from ..config import settings
from ..observability import get_logger

logger = get_logger(__name__)

_LARK_CLI_BIN = "lark-cli"


def is_lark_cli_available() -> bool:
    """检测本机是否装了 lark-cli。软依赖判定，避免 subprocess 拉起后才发现 ENOENT。"""
    return shutil.which(_LARK_CLI_BIN) is not None


def _parse_chat_ids(raw: str) -> list[str]:
    """settings.lark_cli_chat_ids 是逗号分隔字符串，运行时 split + 去空。

    用 str 而不是 list[str] 是为了绕开 pydantic-settings 对 list 字段强制 JSON 解析的坑
    （env 变量必须写成 '["oc_xxx"]' 命令行不友好）。
    """
    return [c.strip() for c in raw.split(",") if c.strip()]


def send_via_lark_cli(text: str, chat_ids: Iterable[str]) -> bool:
    """通过 lark-cli OpenAPI 发送一条文本消息到指定群。

    Args:
        text: 消息正文（已渲染好的纯文本；事件层拼模板）
        chat_ids: 目标群 chat_id 列表（oc_xxx）；可传 list/tuple/generator

    Returns:
        True = 所有 chat_id 全部发送成功；False = 任一失败或 cli 不可用
    """
    chat_id_list = [c for c in chat_ids if c]
    if not chat_id_list:
        logger.debug("notify.lark_cli: skipped (empty chat_ids)")
        return False

    if not is_lark_cli_available():
        # cli 未装 → 软失败，让 _send 走 webhook 兜底
        logger.debug("notify.lark_cli: skipped (lark-cli binary not found)")
        return False

    timeout = settings.lark_cli_timeout_seconds
    all_ok = True

    for chat_id in chat_id_list:
        argv = [
            _LARK_CLI_BIN,
            "im",
            "+messages-send",
            "--as",
            "user",
            "--chat-id",
            chat_id,
            "--text",
            text,
        ]
        try:
            result = subprocess.run(  # noqa: S603 — argv list 形式，无 shell 注入风险
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "notify.lark_cli: subprocess timeout",
                extra={"event": "lark_cli_timeout", "chat_id": chat_id, "timeout": timeout},
            )
            all_ok = False
            continue
        except FileNotFoundError:
            # 极端竞态：is_lark_cli_available 后 cli 被删了
            logger.warning(
                "notify.lark_cli: binary disappeared",
                extra={"event": "lark_cli_not_found", "chat_id": chat_id},
            )
            return False
        except Exception as e:  # 兜底，与 webhook.send 第二道防线对齐
            logger.warning(
                "notify.lark_cli: unexpected subprocess error",
                extra={"event": "lark_cli_unexpected", "chat_id": chat_id, "error": str(e)},
                exc_info=True,
            )
            all_ok = False
            continue

        if result.returncode != 0:
            logger.warning(
                "notify.lark_cli: non-zero exit",
                extra={
                    "event": "lark_cli_nonzero_exit",
                    "chat_id": chat_id,
                    "returncode": result.returncode,
                    "stderr": (result.stderr or "")[:200],
                },
            )
            all_ok = False
            continue

        # 解析 stdout JSON，确认 ok=true 且 data.message_id 存在
        # lark-cli 实证输出形如 {"ok": true, "data": {"message_id": "om_xxx", ...}, "_notice": {...}}
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as e:
            logger.warning(
                "notify.lark_cli: parse json failed",
                extra={
                    "event": "lark_cli_parse_failed",
                    "chat_id": chat_id,
                    "error": str(e),
                    "stdout_head": (result.stdout or "")[:200],
                },
            )
            all_ok = False
            continue

        if not data.get("ok"):
            logger.warning(
                "notify.lark_cli: business-fail",
                extra={
                    "event": "lark_cli_business_fail",
                    "chat_id": chat_id,
                    "body": str(data)[:200],
                },
            )
            all_ok = False
            continue

        msg_id = (data.get("data") or {}).get("message_id")
        if not msg_id:
            logger.warning(
                "notify.lark_cli: no message_id in response",
                extra={"event": "lark_cli_no_msg_id", "chat_id": chat_id, "body": str(data)[:200]},
            )
            all_ok = False
            continue

        logger.debug(
            "notify.lark_cli: send ok",
            extra={"event": "lark_cli_send_ok", "chat_id": chat_id, "message_id": msg_id},
        )

    return all_ok
