"""ClaudeCliDriver 验收 —— 本地 claude CLI 作为 LLM 后端。

离线确定性：用真实信封样本测解析；用 mock subprocess 测命令构建与端到端，
不在 CI 真 spawn claude（那需登录态 + 花额度 + 不确定）。
"""
from __future__ import annotations

import asyncio

import pytest

from ai_ops.config import settings
from ai_ops.content.generator import (
    ClaudeCliDriver,
    _parse_cli_envelope,
    get_driver,
)

# 真实跑 `claude -p --output-format json` 抓到的成功信封（result 带 ```json fence）
SUCCESS_ENVELOPE = (
    b'{"type":"result","subtype":"success","is_error":false,'
    b'"result":"```json\\n{\\"ok\\": true}\\n```","session_id":"x","total_cost_usd":0.02}'
)
ERROR_ENVELOPE = (
    b'{"type":"result","subtype":"error_max_turns","is_error":true,'
    b'"result":"hit limit"}'
)


# ---------------------------------------------------------------------------
# 信封解析
# ---------------------------------------------------------------------------
def test_parse_envelope_success():
    out = _parse_cli_envelope(SUCCESS_ENVELOPE)
    assert out == "```json\n{\"ok\": true}\n```"


def test_parse_envelope_error_raises():
    with pytest.raises(RuntimeError, match="返回错误"):
        _parse_cli_envelope(ERROR_ENVELOPE)


def test_parse_envelope_empty_raises():
    with pytest.raises(RuntimeError, match="无输出"):
        _parse_cli_envelope(b"   ")


def test_parse_envelope_non_json_raises():
    with pytest.raises(RuntimeError, match="非 JSON"):
        _parse_cli_envelope(b"command not found: claude")


# ---------------------------------------------------------------------------
# get_driver 路由
# ---------------------------------------------------------------------------
def test_get_driver_routes_to_claude_cli(monkeypatch):
    monkeypatch.setattr(settings, "llm_default", "claude_cli")
    assert isinstance(get_driver(), ClaudeCliDriver)


# ---------------------------------------------------------------------------
# complete() 端到端（mock subprocess）
# ---------------------------------------------------------------------------
class _FakeProc:
    def __init__(self, stdout: bytes, returncode: int = 0, stderr: bytes = b""):
        self._stdout, self.returncode, self._stderr = stdout, returncode, stderr
        self.sent_stdin: bytes | None = None

    async def communicate(self, data: bytes | None = None):
        self.sent_stdin = data
        return self._stdout, self._stderr

    def kill(self):
        pass


async def test_complete_builds_cmd_and_parses(monkeypatch):
    captured = {}

    async def fake_exec(*cmd, **kw):
        captured["cmd"] = cmd
        return _FakeProc(SUCCESS_ENVELOPE)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(settings, "claude_cli_model", "sonnet")

    out = await ClaudeCliDriver().complete("我是系统提示", "我是用户输入")
    assert out == "```json\n{\"ok\": true}\n```"

    cmd = captured["cmd"]
    assert cmd[0] == settings.claude_cli_bin
    assert "-p" in cmd and "--output-format" in cmd and "json" in cmd
    # system 走 --system-prompt
    assert "--system-prompt" in cmd
    assert "我是系统提示" in cmd
    # model 透传
    assert "--model" in cmd and "sonnet" in cmd


async def test_complete_passes_user_via_stdin(monkeypatch):
    proc = _FakeProc(SUCCESS_ENVELOPE)

    async def fake_exec(*cmd, **kw):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await ClaudeCliDriver().complete("sys", "用户问题内容")
    assert proc.sent_stdin == "用户问题内容".encode("utf-8")  # user 经 stdin 注入


async def test_complete_nonzero_exit_raises(monkeypatch):
    async def fake_exec(*cmd, **kw):
        return _FakeProc(b"", returncode=1, stderr=b"boom")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(RuntimeError, match="退出码 1"):
        await ClaudeCliDriver().complete("sys", "user")
