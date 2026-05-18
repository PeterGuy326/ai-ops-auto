"""Task · notify lark-cli 后端单测。

覆盖：
  - is_lark_cli_available：shutil.which mock
  - send_via_lark_cli：subprocess.run 各种情况（成功 / cli 未装 / 非零退出 / timeout / JSON 解析失败 / business-fail）
  - 多 chat_id：调用次数 + 任一失败整体 False
  - _send 分发：backend = lark_cli / webhook / both / 大小写不敏感
  - chat_ids 解析：逗号分隔 + 空白容错
"""
from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from ai_ops.notify import _send, dedup, lark_cli, publish_success


# ============================================================================
# Fixtures
# ============================================================================
@pytest.fixture(autouse=True)
def _reset_dedup():
    dedup.reset_for_test()
    yield
    dedup.reset_for_test()


def _ok_completed(msg_id: str = "om_test_123") -> MagicMock:
    """构造一个 lark-cli 成功返回的 subprocess.CompletedProcess mock。"""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = 0
    cp.stdout = json.dumps({"ok": True, "data": {"message_id": msg_id, "chat_id": "oc_x"}})
    cp.stderr = ""
    return cp


# ============================================================================
# is_lark_cli_available
# ============================================================================
def test_is_lark_cli_available_true_when_which_finds_it():
    with patch("ai_ops.notify.lark_cli.shutil.which", return_value="/usr/local/bin/lark-cli"):
        assert lark_cli.is_lark_cli_available() is True


def test_is_lark_cli_available_false_when_which_returns_none():
    with patch("ai_ops.notify.lark_cli.shutil.which", return_value=None):
        assert lark_cli.is_lark_cli_available() is False


# ============================================================================
# send_via_lark_cli — 核心 7 项
# ============================================================================
def test_lark_cli_send_calls_subprocess_with_correct_argv():
    """验证 subprocess.run 拿到的 argv 完全正确（--as user / --chat-id / --text 全齐）。"""
    with patch("ai_ops.notify.lark_cli.shutil.which", return_value="/x/lark-cli"), \
         patch("ai_ops.notify.lark_cli.subprocess.run", return_value=_ok_completed()) as m_run:
        ok = lark_cli.send_via_lark_cli("hello 你好", ["oc_abc"])
    assert ok is True
    assert m_run.call_count == 1
    argv = m_run.call_args.args[0]
    assert argv[0] == "lark-cli"
    assert argv[1] == "im"
    assert argv[2] == "+messages-send"
    assert "--as" in argv and argv[argv.index("--as") + 1] == "user"
    assert "--chat-id" in argv and argv[argv.index("--chat-id") + 1] == "oc_abc"
    assert "--text" in argv and argv[argv.index("--text") + 1] == "hello 你好"
    # 安全红线：不能用 shell=True
    assert m_run.call_args.kwargs.get("shell", False) is False


def test_lark_cli_send_returns_false_when_cli_not_installed():
    """shutil.which 返 None → 软依赖跳过，不应该真调 subprocess。"""
    with patch("ai_ops.notify.lark_cli.shutil.which", return_value=None), \
         patch("ai_ops.notify.lark_cli.subprocess.run") as m_run:
        ok = lark_cli.send_via_lark_cli("hi", ["oc_x"])
    assert ok is False
    m_run.assert_not_called()


def test_lark_cli_send_returns_false_on_nonzero_exit():
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = 1
    cp.stdout = ""
    cp.stderr = "permission denied"
    with patch("ai_ops.notify.lark_cli.shutil.which", return_value="/x"), \
         patch("ai_ops.notify.lark_cli.subprocess.run", return_value=cp):
        ok = lark_cli.send_via_lark_cli("hi", ["oc_x"])
    assert ok is False


def test_lark_cli_send_multi_chat_ids_calls_subprocess_per_id():
    """传 2 个 chat_id → subprocess.run 被调 2 次，每次带不同 chat_id。"""
    with patch("ai_ops.notify.lark_cli.shutil.which", return_value="/x"), \
         patch(
             "ai_ops.notify.lark_cli.subprocess.run",
             side_effect=[_ok_completed("om_1"), _ok_completed("om_2")],
         ) as m_run:
        ok = lark_cli.send_via_lark_cli("hi", ["oc_a", "oc_b"])
    assert ok is True
    assert m_run.call_count == 2
    chat_ids_seen = []
    for call in m_run.call_args_list:
        argv = call.args[0]
        chat_ids_seen.append(argv[argv.index("--chat-id") + 1])
    assert chat_ids_seen == ["oc_a", "oc_b"]


def test_lark_cli_send_handles_timeout():
    """subprocess.TimeoutExpired 必须被吞掉，返 False。"""
    with patch("ai_ops.notify.lark_cli.shutil.which", return_value="/x"), \
         patch(
             "ai_ops.notify.lark_cli.subprocess.run",
             side_effect=subprocess.TimeoutExpired(cmd="lark-cli", timeout=15),
         ):
        ok = lark_cli.send_via_lark_cli("hi", ["oc_x"])
    assert ok is False


def test_lark_cli_send_handles_parse_failure():
    """stdout 不是合法 JSON → 解析失败吞掉返 False，不抛。"""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = 0
    cp.stdout = "not json !!!"
    cp.stderr = ""
    with patch("ai_ops.notify.lark_cli.shutil.which", return_value="/x"), \
         patch("ai_ops.notify.lark_cli.subprocess.run", return_value=cp):
        ok = lark_cli.send_via_lark_cli("hi", ["oc_x"])
    assert ok is False


def test_lark_cli_send_handles_business_fail():
    """ok=false 业务失败 → False。"""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = 0
    cp.stdout = json.dumps({"ok": False, "error": {"message": "chat not found"}})
    cp.stderr = ""
    with patch("ai_ops.notify.lark_cli.shutil.which", return_value="/x"), \
         patch("ai_ops.notify.lark_cli.subprocess.run", return_value=cp):
        ok = lark_cli.send_via_lark_cli("hi", ["oc_x"])
    assert ok is False


def test_lark_cli_send_empty_chat_ids_returns_false():
    """空 chat_ids → 直接返 False，不调 subprocess。"""
    with patch("ai_ops.notify.lark_cli.subprocess.run") as m_run:
        ok = lark_cli.send_via_lark_cli("hi", [])
    assert ok is False
    m_run.assert_not_called()


def test_lark_cli_send_multi_partial_fail_returns_false():
    """两路中一路失败 → 整体 False（all_ok 语义）。"""
    cp_fail = MagicMock(spec=subprocess.CompletedProcess)
    cp_fail.returncode = 1
    cp_fail.stdout = ""
    cp_fail.stderr = "err"
    with patch("ai_ops.notify.lark_cli.shutil.which", return_value="/x"), \
         patch(
             "ai_ops.notify.lark_cli.subprocess.run",
             side_effect=[_ok_completed("om_1"), cp_fail],
         ):
        ok = lark_cli.send_via_lark_cli("hi", ["oc_ok", "oc_bad"])
    assert ok is False


# ============================================================================
# _send 分发：按 backend 切换
# ============================================================================
def test_send_backend_lark_cli_only_calls_lark_cli(monkeypatch):
    """backend='lark_cli' 时只调 lark_cli.send_via_lark_cli，不调 webhook.send。"""
    from ai_ops import config as cfg
    monkeypatch.setattr(cfg.settings, "notify_backend", "lark_cli")
    monkeypatch.setattr(cfg.settings, "lark_cli_chat_ids", "oc_test_only")
    with patch("ai_ops.notify.lark_cli.send_via_lark_cli", return_value=True) as m_cli, \
         patch("ai_ops.notify.webhook.send", return_value=True) as m_hook:
        ok = _send("hello")
    assert ok is True
    m_cli.assert_called_once()
    # 验证传入的 chat_ids 是解析后的 list
    args, _ = m_cli.call_args
    assert args[0] == "hello"
    assert args[1] == ["oc_test_only"]
    m_hook.assert_not_called()


def test_send_backend_webhook_only_calls_webhook(monkeypatch):
    """backend='webhook' 时只调 webhook.send，不进 lark_cli 分支。"""
    from ai_ops import config as cfg
    monkeypatch.setattr(cfg.settings, "notify_backend", "webhook")
    with patch("ai_ops.notify.lark_cli.send_via_lark_cli", return_value=True) as m_cli, \
         patch("ai_ops.notify.webhook.send", return_value=True) as m_hook:
        ok = _send("hello")
    assert ok is True
    m_cli.assert_not_called()
    m_hook.assert_called_once_with("hello")


def test_send_backend_both_calls_both(monkeypatch):
    """backend='both' 时两路都调，任一成功 → True。"""
    from ai_ops import config as cfg
    monkeypatch.setattr(cfg.settings, "notify_backend", "both")
    monkeypatch.setattr(cfg.settings, "lark_cli_chat_ids", "oc_a,oc_b")
    with patch("ai_ops.notify.lark_cli.send_via_lark_cli", return_value=True) as m_cli, \
         patch("ai_ops.notify.webhook.send", return_value=False) as m_hook:
        ok = _send("hello")
    assert ok is True  # 任一成功即 True
    m_cli.assert_called_once()
    m_hook.assert_called_once()


def test_send_backend_case_insensitive(monkeypatch):
    """backend 字段大小写不敏感（'LARK_CLI' / 'Both' / 'WebHook' 都能识别）。"""
    from ai_ops import config as cfg
    monkeypatch.setattr(cfg.settings, "notify_backend", "LARK_CLI")
    monkeypatch.setattr(cfg.settings, "lark_cli_chat_ids", "oc_x")
    with patch("ai_ops.notify.lark_cli.send_via_lark_cli", return_value=True) as m_cli, \
         patch("ai_ops.notify.webhook.send", return_value=True) as m_hook:
        _send("hi")
    m_cli.assert_called_once()
    m_hook.assert_not_called()


def test_send_lark_cli_empty_chat_ids_skips_cli_path(monkeypatch):
    """settings.lark_cli_chat_ids 为空时，lark_cli 分支跳过不调用，避免无意义 subprocess。"""
    from ai_ops import config as cfg
    monkeypatch.setattr(cfg.settings, "notify_backend", "lark_cli")
    monkeypatch.setattr(cfg.settings, "lark_cli_chat_ids", "")
    with patch("ai_ops.notify.lark_cli.send_via_lark_cli", return_value=True) as m_cli, \
         patch("ai_ops.notify.webhook.send", return_value=False) as m_hook:
        ok = _send("hi")
    assert ok is False
    m_cli.assert_not_called()
    m_hook.assert_not_called()  # backend=lark_cli 不走 webhook


# ============================================================================
# 事件函数 → _send → lark_cli 端到端集成（不真发，只验证 dispatch 链）
# ============================================================================
def test_publish_success_dispatches_through_lark_cli_when_backend_set(monkeypatch):
    """publish_success → _send → lark_cli.send_via_lark_cli 整条链路打通。"""
    from ai_ops import config as cfg
    monkeypatch.setattr(cfg.settings, "notify_backend", "lark_cli")
    monkeypatch.setattr(cfg.settings, "lark_cli_chat_ids", "oc_test")
    with patch("ai_ops.notify.lark_cli.send_via_lark_cli", return_value=True) as m_cli:
        publish_success({
            "id": 1, "account_id": 1, "platform": "xhs",
            "platform_url": "https://x.test/p/1", "title": "t",
        })
    m_cli.assert_called_once()
    text, chat_ids = m_cli.call_args.args
    assert "account_id=1" in text
    assert "xhs" in text
    assert chat_ids == ["oc_test"]


# ============================================================================
# chat_ids 解析
# ============================================================================
def test_parse_chat_ids_handles_whitespace_and_empty_segments():
    assert lark_cli._parse_chat_ids("oc_a , oc_b ,, oc_c") == ["oc_a", "oc_b", "oc_c"]
    assert lark_cli._parse_chat_ids("") == []
    assert lark_cli._parse_chat_ids("  ") == []
    assert lark_cli._parse_chat_ids("oc_solo") == ["oc_solo"]
