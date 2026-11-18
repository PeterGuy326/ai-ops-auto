"""doctor 的配置路径必须与全项目同一真相源。

背景：合并 #70/#71 后 web/app.py 与 easel/local_agents.py 都已统一走
easel.openclaw_workspace.config_path()，doctor._openclaw_config_path() 仍自己
解析 EASEL_OPENCLAW_STATE_DIR。两处语义有细微差别——环境变量是空串时
doctor 返回 Path("")/"openclaw.json"（相对路径，随当前工作目录漂移），
config_path() 会正确回落默认 profile 目录。统一委托后由本文件钉住。
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from easel import openclaw_workspace as ws


def test_doctor_delegates_to_config_path(tmp_path, monkeypatch):
    import easel.commands.doctor as doctor

    monkeypatch.setenv("EASEL_OPENCLAW_STATE_DIR", str(tmp_path))
    assert doctor._openclaw_config_path() == ws.config_path()
    assert doctor._openclaw_config_path() == tmp_path / "openclaw.json"


def test_doctor_empty_env_falls_back_like_config_path(monkeypatch):
    """空串覆盖：doctor 旧实现返回 Path("")（相对路径），config_path() 回落默认目录。"""
    import easel.commands.doctor as doctor

    monkeypatch.setenv("EASEL_OPENCLAW_STATE_DIR", "")
    assert doctor._openclaw_config_path() == ws.config_path()
    assert doctor._openclaw_config_path() == Path.home() / ".openclaw-easel" / "openclaw.json"


def test_doctor_no_direct_path_concat():
    """源码级：doctor 不允许再出现直连 .openclaw-easel 的路径拼接。"""
    src = (PROJECT_ROOT / "easel" / "commands" / "doctor.py").read_text(encoding="utf-8")
    assert 'Path.home() / ".openclaw-easel"' not in src
    assert "Path.home() / '.openclaw-easel'" not in src


def _write_openclaw_config(path, primary):
    path.write_text(
        json.dumps({
            "agents": {"defaults": {"model": {"primary": primary}}},
            "models": {"providers": {primary.split('/', 1)[0]: {"baseURL": "http://localhost"}}},
        }),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("primary", "cli"),
    [
        ("claude-cli/claude-sonnet-4-5", "claude"),
        ("google-gemini-cli/gemini-2.5-pro", "gemini"),
    ],
)
def test_doctor_accepts_logged_in_agent_cli_without_api_key(tmp_path, monkeypatch, primary, cli):
    import easel.commands.doctor as doctor

    monkeypatch.setenv("EASEL_OPENCLAW_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(doctor, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=REPLACE_ME\n", encoding="utf-8")
    _write_openclaw_config(tmp_path / "openclaw.json", primary)
    monkeypatch.setattr(doctor.shutil, "which", lambda cmd: f"/usr/bin/{cmd}" if cmd == cli else None)

    assert doctor._env_key_valid() is True
    assert doctor._primary_model_routable() == (True, "")


def test_doctor_reports_missing_agent_cli(tmp_path, monkeypatch):
    import easel.commands.doctor as doctor

    monkeypatch.setenv("EASEL_OPENCLAW_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(doctor, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=REPLACE_ME\n", encoding="utf-8")
    _write_openclaw_config(tmp_path / "openclaw.json", "claude-cli/claude-sonnet-4-5")
    monkeypatch.setattr(doctor.shutil, "which", lambda _cmd: None)

    ok, detail = doctor._primary_model_routable()
    assert ok is False
    assert "Claude Code CLI" in detail
