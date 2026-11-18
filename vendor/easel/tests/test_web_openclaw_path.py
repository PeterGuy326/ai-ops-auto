"""#62 回归：web 模块读写 openclaw.json 必须走 easel.openclaw_workspace.config_path()。

背景（#62）：web/app.py 多处直接 Path.home()/'.openclaw-easel'/openclaw.json，
测试夹具 monkeypatch EASEL_OPENCLAW_STATE_DIR（workspace 测试的既有约定）或
patch Path.home 都拦不住这些直连点 —— 跑测试可能污染用户真实配置（复现：
主模型被测试值覆盖）。统一收口到 config_path() 后，既有 env 覆盖机制一处
生效，测试天然隔离，用户配置不再可能被测试碰。
"""
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "web"))

import pytest

from easel import openclaw_workspace as ws
import web.app as web


def test_web_module_uses_config_path_helper():
    """web/app.py、easel/local_agents.py 源码里不允许再出现直连
    Path.home()/.openclaw-easel 的路径拼接（后者是合并 #70 后新增的同类直连点，
    #71 原本只扫了 web/app.py，没扫到这个后加的模块）。"""
    for rel in ("web/app.py", "easel/local_agents.py"):
        src = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        assert "Path.home() / '.openclaw-easel'" not in src, \
            f"{rel} 直连 .openclaw-easel 目录 —— 改用 easel.openclaw_workspace.config_path()"
        assert 'Path.home() / ".openclaw-easel"' not in src, \
            f"{rel} 直连 .openclaw-easel 目录 —— 改用 easel.openclaw_workspace.config_path()"


def test_local_agents_respects_state_dir_override(tmp_path, monkeypatch):
    """local_agents 曾有自己的一套 EASEL_OPENCLAW_CONFIG 覆盖机制，跟
    EASEL_OPENCLAW_STATE_DIR 不是同一个变量：真实场景下用户设了
    EASEL_OPENCLAW_STATE_DIR 换配置目录，detect_local_agents() 的 configured
    判断却仍读旧默认位置——而 web/app.py 的写入（declare provider / 切主模型）
    已经走 config_path()，读写对不上，「一键接入」会显示没生效或重复接入。"""
    import easel.local_agents as la

    monkeypatch.setenv("EASEL_OPENCLAW_STATE_DIR", str(tmp_path))
    oc = tmp_path / "openclaw.json"
    oc.write_text(json.dumps({"models": {"providers": {"anthropic": {"baseUrl": "x"}}}}),
                  encoding="utf-8")
    monkeypatch.setattr(la.shutil, "which", lambda c: "/usr/bin/claude" if c == "claude" else None)
    by_id = {a["id"]: a for a in la.detect_local_agents()}
    assert by_id["claude-code"]["configured"] is True


def test_config_path_respects_state_dir_override(tmp_path, monkeypatch):
    """收口后，EASEL_OPENCLAW_STATE_DIR 一处覆盖全部读写（测试隔离的钥匙）。"""
    monkeypatch.setenv("EASEL_OPENCLAW_STATE_DIR", str(tmp_path))
    assert ws.config_path() == tmp_path / "openclaw.json"
    assert ws.state_dir() == tmp_path


def test_sync_writes_via_state_dir_override(tmp_path, monkeypatch):
    """端到端：设了 EASEL_OPENCLAW_STATE_DIR 后，保存操作只写沙箱里的 openclaw.json。"""
    from starlette.testclient import TestClient

    monkeypatch.setenv("EASEL_OPENCLAW_STATE_DIR", str(tmp_path))
    oc = tmp_path / "openclaw.json"
    oc.write_text(json.dumps({
        "models": {"providers": {"openai": {
            "baseUrl": "https://user-real.example.com", "apiKey": "user-real-key",
            "models": [{"id": "user-model"}]}}},
        "agents": {"defaults": {"model": {"primary": "openai/user-model"}}},
    }, ensure_ascii=False), encoding="utf-8")

    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=k\nOPENAI_BASE_URL=https://api.openai.com/v1\n", encoding="utf-8")
    monkeypatch.setattr(web, "ENV_FILE", env_file)

    local = "http://127.0.0.1:7860"
    with TestClient(web.app, base_url=local, client=("127.0.0.1", 51234),
                    headers={"Origin": local}) as c:
        resp = c.post("/api/settings/models/save", json={"channel": "chat", "rows": [
            {"slot": "openai", "model": "gpt-4o-mini",
             "baseUrl": "https://api.openai.com/v1", "key": "sk-fresh", "primary": True}]})
    assert resp.status_code == 200, resp.text[:300]

    data = json.loads(oc.read_text(encoding="utf-8"))
    prov = data["models"]["providers"]["openai"]
    assert prov["apiKey"] == "sk-fresh"        # 写进了沙箱
    assert data["agents"]["defaults"]["model"]["primary"] == "openai/gpt-4o-mini"
