"""ai_image.py 的 Agnes AI provider 回归测试（issue #51）。

Agnes 没有 /images/edits 与 /images/variations：文生图 / 图生图 / 变体统一打
/images/generations，输入图放 extra_body.image，size 用档位（1K/2K/3K/4K）+
独立 ratio，n>1 必须客户端循环（顶层带 n / response_format 会被 400）。

不发任何真实网络请求：http_post 被替换为捕获桩，只断言请求端点与 payload 形状。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHARED_SCRIPTS = PROJECT_ROOT / "skills" / "shared" / "scripts"
sys.path.insert(0, str(SHARED_SCRIPTS))

import ai_image  # noqa: E402

AGNES_BASE = "https://api.agnes-ai.cn/v1"
OPENAI_BASE = "https://api.openai.com/v1"
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


class Calls:
    """捕获 http_post 调用，返回一张 1x1 PNG（url 路径走不到下载，用 b64_json）。"""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, url, api_key, payload, timeout=120):  # noqa: D102
        self.requests.append((url, payload))
        return {"data": [{"b64_json": "aGVsbG8=", "url": None}]}

    @property
    def urls(self) -> list[str]:
        return [u for u, _ in self.requests]

    @property
    def payloads(self) -> list[dict]:
        return [p for _, p in self.requests]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离配置 + 输出目录 + 假网络。返回 (calls, input_png)。"""
    calls = Calls()
    monkeypatch.setattr(ai_image, "http_post", calls)
    monkeypatch.setenv("IMG_BASE_URL", AGNES_BASE)
    monkeypatch.setenv("IMG_MODEL", "agnes-image-2.1-flash")
    monkeypatch.setenv("IMG_API_KEY", "sk-test")
    for name in ("OPENAI_BASE_URL", "OPENAI_API_BASE", "BASE_URL",
                 "OPENAI_IMAGE_MODEL", "IMAGE_MODEL", "OPENAI_MODEL",
                 "OPENAI_API_KEY", "API_KEY", "IMG_API_VERSION", "IMG_NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    png = tmp_path / "in.png"
    png.write_bytes(PNG_BYTES)
    return calls, png


def _out(tmp_path) -> str:
    return str(tmp_path / "out" / "img.png")


# ── provider 检测 ─────────────────────────────────────────

@pytest.mark.parametrize("base", [
    "https://api.agnes-ai.cn/v1",
    "https://AGNES-AI.CN/v1",
])
def test_detect_provider_matches_agnes_host(base: str) -> None:
    assert ai_image.detect_provider(base) == "agnes"


@pytest.mark.parametrize("base", [
    "https://api.openai.com/v1",
    "https://token.lightvela.com/v1",
    "https://api.apimart.ai/v1",
])
def test_detect_provider_defaults_openai(base: str) -> None:
    assert ai_image.detect_provider(base) == "openai"


# ── size 归一 ────────────────────────────────────────────

def test_split_size_pixel_maps_to_tier_and_ratio() -> None:
    assert ai_image.agnes_split_size("1024x1024", "2k") == ("1K", "1:1")
    assert ai_image.agnes_split_size("1920x1080", "2k") == ("2K", "16:9")
    assert ai_image.agnes_split_size("3840x2160", "1k") == ("3K", "16:9")


def test_split_size_ratio_uses_resolution_tier() -> None:
    assert ai_image.agnes_split_size("16:9", "2k") == ("2K", "16:9")
    assert ai_image.agnes_split_size("1:1", "1k") == ("1K", "1:1")


def test_split_size_unknown_pixel_fails() -> None:
    with pytest.raises(SystemExit):
        ai_image.agnes_split_size("not-a-size", "2k")


def test_split_size_unlisted_pixel_is_reduced() -> None:
    # 官方未列的精确尺寸（如 1920x1080）按边长推档位、按最大公约数化简比例。
    assert ai_image.agnes_split_size("1920x1080", "2k") == ("2K", "16:9")
    assert ai_image.agnes_split_size("800x600", "1k") == ("1K", "4:3")


# ── 三个子命令的请求形状（issue #51 核心）─────────────────

def test_text2img_posts_generations_without_top_level_n_or_response_format(
    env, tmp_path, monkeypatch
) -> None:
    calls, _ = env
    monkeypatch.chdir(tmp_path)
    args = ai_image.parse_args(
        ["text2img", "--prompt", "cat", "--output", _out(tmp_path)]
    )
    ai_image.cmd_text2img(args)

    assert calls.urls == [f"{AGNES_BASE}/images/generations"]
    payload = calls.payloads[0]
    assert "n" not in payload, "顶层 n 会触发 Agnes 400（n 必须为 1）"
    assert "response_format" not in payload, "response_format 必须放 extra_body"
    assert payload["extra_body"]["response_format"] == "url"
    assert payload["size"] in {"1K", "2K", "3K", "4K"}
    assert "image" not in payload["extra_body"]


def test_img2img_posts_generations_with_extra_body_image(
    env, tmp_path, monkeypatch
) -> None:
    """修复前：打 /images/edits → 404。修复后：generations + extra_body.image。"""
    calls, png = env
    monkeypatch.chdir(tmp_path)
    args = ai_image.parse_args(
        ["img2img", "--prompt", "cyberpunk", "--image", str(png),
         "--output", _out(tmp_path), "--size", "16:9", "--resolution", "2k"]
    )
    ai_image.cmd_img2img(args)

    assert "/images/edits" not in calls.urls[0]
    assert calls.urls == [f"{AGNES_BASE}/images/generations"]
    payload = calls.payloads[0]
    images = payload["extra_body"].get("image")
    assert isinstance(images, list) and len(images) == 1
    assert images[0].startswith("data:image/png;base64,")
    assert payload["size"] == "2K" and payload["ratio"] == "16:9"


def test_img2img_rejects_mask_on_agnes(env, tmp_path, monkeypatch) -> None:
    calls, png = env
    monkeypatch.chdir(tmp_path)
    args = ai_image.parse_args(
        ["img2img", "--prompt", "x", "--image", str(png), "--mask", str(png),
         "--output", _out(tmp_path)]
    )
    with pytest.raises(SystemExit):
        ai_image.cmd_img2img(args)
    assert calls.requests == []


def test_variations_posts_generations_with_reference(
    env, tmp_path, monkeypatch
) -> None:
    """修复前：打 /images/variations → 404。修复后：generations + extra_body.image。"""
    calls, png = env
    monkeypatch.chdir(tmp_path)
    args = ai_image.parse_args(
        ["variations", "--image", str(png), "--output", _out(tmp_path)]
    )
    ai_image.cmd_variations(args)

    assert "/images/variations" not in calls.urls[0]
    assert calls.urls == [f"{AGNES_BASE}/images/generations"]
    payload = calls.payloads[0]
    assert payload["extra_body"]["image"][0].startswith("data:image/png;base64,")
    assert payload["prompt"], "变体需要兜底 prompt"


def test_multi_count_loops_one_request_per_image(
    env, tmp_path, monkeypatch
) -> None:
    """Agnes 强制 n=1：--n 3 必须拆成 3 次单张请求，而不是顶层带 n。"""
    calls, png = env
    monkeypatch.chdir(tmp_path)
    args = ai_image.parse_args(
        ["img2img", "--prompt", "x", "--image", str(png),
         "--output", _out(tmp_path), "--n", "3"]
    )
    ai_image.cmd_img2img(args)

    assert len(calls.requests) == 3
    for payload in calls.payloads:
        assert "n" not in payload


# ── 既有 provider 零影响（p2p 保护）────────────────────────

def test_openai_sync_paths_unchanged(env, tmp_path, monkeypatch) -> None:
    """标准 OpenAI base_url 仍走 /images/edits、顶层 n —— 零影响保护。"""
    calls, png = env
    monkeypatch.setenv("IMG_BASE_URL", OPENAI_BASE)
    monkeypatch.chdir(tmp_path)

    args = ai_image.parse_args(
        ["text2img", "--prompt", "cat", "--output", _out(tmp_path), "--n", "2"]
    )
    ai_image.cmd_text2img(args)
    assert calls.urls[-1] == f"{OPENAI_BASE}/images/generations"
    assert calls.payloads[-1]["n"] == 2
    assert calls.payloads[-1]["size"] == "1024x1024"

    # img2img 的 multipart 路径：桩掉底层 _open，断言端点仍是 /images/edits。
    captured: dict = {}

    class _Resp:
        def read(self) -> bytes:
            return b'{"data": [{"b64_json": "aGk="}]}'

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        return _Resp()

    monkeypatch.setattr(ai_image, "_open", fake_open)
    args = ai_image.parse_args(
        ["img2img", "--prompt", "x", "--image", str(png), "--output", _out(tmp_path)]
    )
    ai_image.cmd_img2img(args)
    assert captured["url"] == f"{OPENAI_BASE}/images/edits"


# ── detect_provider 域名匹配的防误判（审计补） ──────────────────

def test_detect_provider_rejects_lookalike_domains():
    """含 agnes 字母序列的无关域名不得误判成 Agnes（子串匹配的历史 bug）。"""
    for url in ("https://api.stagneschurch.com/v1", "https://api.magnes.com/v1",
                "https://notagnes.example.com/v1"):
        assert ai_image.detect_provider(url) == "openai", url


def test_detect_provider_accepts_known_agnes_domains():
    for url in ("https://api.agnes-ai.cn/v1", "https://apihub.agnes-ai.com/v1",
                "https://x.agnes-ai.cn/v1"):
        assert ai_image.detect_provider(url) == "agnes", url
