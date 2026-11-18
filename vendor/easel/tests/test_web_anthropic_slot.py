"""Web 设置面板 anthropic（Claude）通道的两处回归。

背景（issue 型问题，非新增特性）：
  ① `bash setup.sh` 通过 oc_write_anthropic 把 anthropic 写进 openclaw.json，
     对话才真正走得到 Claude。但设置面板保存 anthropic 槽位时**只更新 .env**，
     openclaw.json 的 anthropic provider 从不落盘 —— 界面回显「已配置」、
     没有报错、实际对话仍走旧模型。用户没有任何线索能定位。
  ② 「连通性自测」对所有通道统一发 `GET {base}/models` + `Authorization: Bearer`。
     Anthropic Messages 的鉴权头是 `x-api-key`，模型列表在 `/v1/models`，
     于是 Claude 通道自测**必然失败**，把「配置正确」误导成「Key 无效」。

运行：pytest tests/test_web_anthropic_slot.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "web"))
sys.path.insert(0, str(PROJECT_ROOT / "skills" / "shared" / "scripts"))

import app as web  # noqa: E402

ORIGINAL_ENV = (
    "OPENAI_BASE_URL=https://api.openai.com/v1\n"
    "OPENAI_API_KEY=sk-fak...test\n"
)


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """把 .env 与 openclaw.json 都换到沙箱，绝不碰用户真配置。"""
    env_file = tmp_path / ".env"
    env_file.write_text(ORIGINAL_ENV, encoding="utf-8")
    monkeypatch.setattr(web, "ENV_FILE", env_file)

    oc_dir = tmp_path / ".openclaw-easel"
    oc_dir.mkdir()
    oc = oc_dir / "openclaw.json"
    oc.write_text(json.dumps({
        "models": {"providers": {"openai": {"baseUrl": "https://x/v1", "apiKey": "k",
                                            "models": [{"id": "m"}]}}},
        "agents": {"defaults": {"model": {"primary": "openai/m"}}},
    }, ensure_ascii=False), encoding="utf-8")
    # _sync_anthropic_provider / _sync_openclaw_chat 都按 ~/.openclaw-easel 定位
    monkeypatch.setattr(web.Path, "home", staticmethod(lambda: tmp_path))

    local = "http://127.0.0.1:7860"
    with TestClient(web.app, base_url=local, client=("127.0.0.1", 51234),
                    headers={"Origin": local}) as c:
        c.env_file = env_file
        c.oc_file = oc
        yield c


def _save(client, payload):
    return client.post("/api/settings/models/save", json=payload)


# ---- ① 保存 anthropic 必须落到 openclaw.json ----


def test_anthropic_save_writes_provider_into_openclaw(sandbox):
    """保存 key → openclaw.json 出现 anthropic provider（否则对话根本走不到）。"""
    resp = _save(sandbox, {"channel": "chat", "rows": [
        {"slot": "anthropic", "model": "claude-sonnet-4-6", "key": "sk-ant-test"}]})
    assert resp.status_code == 200, resp.text

    oc = json.loads(sandbox.oc_file.read_text(encoding="utf-8"))
    prov = oc["models"]["providers"]["anthropic"]
    assert prov["apiKey"] == "sk-ant-test"
    assert prov["baseUrl"] == "https://api.anthropic.com"
    assert prov["models"] == []
    # 既有的 openai provider 不能被顺手抹掉
    assert oc["models"]["providers"]["openai"]["apiKey"] == "k"
    # 提示语要能解释「为什么刚才没生效」
    assert "anthropic" in resp.json().get("note", "")


def test_anthropic_save_updates_existing_provider_in_place(sandbox):
    """已存在的 anthropic provider 应被就地更新，且不丢其它字段。"""
    data = json.loads(sandbox.oc_file.read_text(encoding="utf-8"))
    data["models"]["providers"]["anthropic"] = {
        "baseUrl": "https://old.example.com", "apiKey": "old-key",
        "timeoutSeconds": 600, "models": []}
    sandbox.oc_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    resp = _save(sandbox, {"channel": "chat", "rows": [
        {"slot": "anthropic", "model": "claude-sonnet-4-6",
         "baseUrl": "https://relay.example.com", "key": "new-key"}]})
    assert resp.status_code == 200, resp.text

    prov = json.loads(sandbox.oc_file.read_text(encoding="utf-8"))["models"]["providers"]["anthropic"]
    assert prov["baseUrl"] == "https://relay.example.com"
    assert prov["apiKey"] == "new-key"
    assert prov["timeoutSeconds"] == 600, "就地更新不该丢掉其它字段"


def test_custom_base_url_persisted_to_env(sandbox):
    """面板此前不接受 anthropic 的 Base URL —— 中转到自建网关无处可填。"""
    resp = _save(sandbox, {"channel": "chat", "rows": [
        {"slot": "anthropic", "model": "claude-sonnet-4-6",
         "baseUrl": "https://relay.example.com", "key": "sk-ant-test"}]})
    assert resp.status_code == 200, resp.text
    env = sandbox.env_file.read_text(encoding="utf-8")
    assert "ANTHROPIC_BASE_URL=https://relay.example.com" in env
    assert "ANTHROPIC_API_KEY=sk-ant-test" in env


def test_openai_slot_still_unchanged(sandbox):
    """零影响保护：openai 槽位的写入行为不能被本次改动带偏。"""
    resp = _save(sandbox, {"channel": "chat", "rows": [
        {"slot": "openai", "model": "gpt-4o", "baseUrl": "https://api.openai.com/v1",
         "key": "sk-new"}]})
    assert resp.status_code == 200, resp.text
    oc = json.loads(sandbox.oc_file.read_text(encoding="utf-8"))
    assert "anthropic" not in oc["models"]["providers"], "没保存 anthropic 却造出了 provider"
    assert oc["models"]["providers"]["openai"]["apiKey"] == "sk-new"


# ---- ② 自测必须按 Anthropic 协议探测 ----


def _capture_probes(monkeypatch):
    """拦住真实网络请求，记录每次探测的 (url, headers)。"""
    seen: list[tuple[str, dict]] = []

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Opener:
        def open(self, rq, timeout=None):
            seen.append((rq.full_url, {k.lower(): v for k, v in dict(rq.headers).items()}))
            return _Resp()

    monkeypatch.setattr(web.urllib.request, "build_opener", lambda *a, **k: _Opener())
    return seen


def test_selftest_uses_anthropic_headers_and_path(sandbox, monkeypatch):
    """anthropic 通道要用 x-api-key + /v1/models，而不是 Bearer + /models。"""
    sandbox.env_file.write_text(
        ORIGINAL_ENV
        + "ANTHROPIC_BASE_URL=https://api.anthropic.com\n"
          "ANTHROPIC_API_KEY=sk-ant-test\n",
        encoding="utf-8")
    seen = _capture_probes(monkeypatch)

    resp = sandbox.post("/api/settings/models/selftest", json={"channel": "chat"})
    assert resp.status_code == 200, resp.text

    anthropic_calls = [(u, h) for u, h in seen if "anthropic" in u]
    assert anthropic_calls, f"没有探测 anthropic 通道：{seen}"
    url, headers = anthropic_calls[0]
    assert url == "https://api.anthropic.com/v1/models"
    assert headers.get("x-api-key") == "sk-ant-test"
    assert headers.get("anthropic-version")


def test_selftest_keeps_openai_shape(sandbox, monkeypatch):
    """零影响保护：OpenAI 兼容通道仍是 Bearer + /models。"""
    seen = _capture_probes(monkeypatch)
    resp = sandbox.post("/api/settings/models/selftest", json={"channel": "chat"})
    assert resp.status_code == 200, resp.text

    openai_calls = [(u, h) for u, h in seen if "api.openai.com" in u]
    assert openai_calls, f"没有探测 openai 通道：{seen}"
    url, headers = openai_calls[0]
    assert url == "https://api.openai.com/v1/models"
    assert headers.get("authorization", "").startswith("Bearer ")


def test_selftest_still_blocks_private_targets(sandbox, monkeypatch):
    """自测会把真 Key 当凭据发出去 —— SSRF 闸不能被本次改动绕开。"""
    sandbox.env_file.write_text(
        ORIGINAL_ENV
        + "ANTHROPIC_BASE_URL=http://169.254.169.254\n"
          "ANTHROPIC_API_KEY=sk-ant-test\n",
        encoding="utf-8")
    seen = _capture_probes(monkeypatch)
    resp = sandbox.post("/api/settings/models/selftest", json={"channel": "chat"})
    assert resp.status_code == 200, resp.text
    assert not any("169.254.169.254" in u for u, _ in seen), "内网目标竟然发了请求"
    hit = [r for r in resp.json()["results"] if "169.254" in r["baseUrl"]]
    assert hit and hit[0]["ok"] is False
