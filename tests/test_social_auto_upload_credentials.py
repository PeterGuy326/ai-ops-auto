"""SAU 登录凭证桥接回归测试。

所有 subprocess 与文件系统状态均为 fake/tmp_path，不连接任何真实平台。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from ai_ops.config import settings
from ai_ops.core.enums import ContentType, Platform
from ai_ops.core.schemas import PublishContent, PublishResult
from ai_ops.publishers import social_auto_upload as sau_mod
from ai_ops.publishers.social_auto_upload import (
    SAU_CREDENTIAL_REF_KEY,
    SAU_CREDENTIAL_REF_PROVIDER,
    SocialAutoUploadPublisher,
)


class _FakeProcess:
    def __init__(
        self,
        returncode: int,
        on_communicate: Callable[[], None] | None = None,
    ) -> None:
        self.returncode = returncode
        self._on_communicate = on_communicate

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._on_communicate is not None:
            self._on_communicate()
        return b"discarded stdout", b"discarded stderr"


def _fake_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    process: _FakeProcess,
) -> list[tuple[tuple[object, ...], dict]]:
    calls: list[tuple[tuple[object, ...], dict]] = []

    async def _create(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(sau_mod.asyncio, "create_subprocess_exec", _create)
    monkeypatch.setattr(sau_mod, "build_subprocess_env", lambda: {})
    return calls


@pytest.mark.parametrize(
    "relative_cookie_path",
    [
        "cookies/douyin_acc_17.json",
        "cookiesFile/douyin/acc_17.json",
    ],
)
@pytest.mark.asyncio
async def test_login_mirrors_cookie_into_passed_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relative_cookie_path: str,
) -> None:
    """当前 cookies/ 与历史 cookiesFile/ 布局都会原地回填 API 持有的 dict。"""
    monkeypatch.setattr(settings, "external_sau_path", tmp_path)
    cookie_path = tmp_path / relative_cookie_path
    cookie_payload = {
        "cookies": [{"name": "sessionid", "value": "test-only-secret"}],
        "origins": [],
    }

    def _write_upstream_cookie() -> None:
        cookie_path.parent.mkdir(parents=True, exist_ok=True)
        cookie_path.write_text(json.dumps(cookie_payload), encoding="utf-8")

    calls = _fake_subprocess(
        monkeypatch,
        _FakeProcess(returncode=0, on_communicate=_write_upstream_cookie),
    )
    publisher = SocialAutoUploadPublisher(Platform.DOUYIN)
    credential = {"stale": "value"}

    assert await publisher.login(17, credential) is True
    assert credential == cookie_payload
    assert publisher.last_login_error is None
    assert calls[0][0][-2:] == ("--account", "acc_17")
    assert calls[0][1]["cwd"] == str(tmp_path)


@pytest.mark.asyncio
async def test_login_without_cookie_uses_reference_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游命令成功但无 JSON 时，以非空引用保留其自管磁盘态。"""
    monkeypatch.setattr(settings, "external_sau_path", tmp_path)
    _fake_subprocess(monkeypatch, _FakeProcess(returncode=0))
    publisher = SocialAutoUploadPublisher(Platform.XIAOHONGSHU)
    credential: dict = {}

    assert await publisher.login(23, credential) is True
    assert credential == {
        SAU_CREDENTIAL_REF_KEY: {
            "provider": SAU_CREDENTIAL_REF_PROVIDER,
            "platform": "xiaohongshu",
            "account_name": "acc_23",
        }
    }

    # publish 前同步必须直接短路，不能创建目录，更不能把 marker 当 cookie 写入。
    publisher._sync_cookie_if_needed(23, credential)
    assert not (tmp_path / "cookies").exists()
    assert not (tmp_path / "cookiesFile").exists()


@pytest.mark.asyncio
async def test_login_invalid_json_fails_closed_without_exposing_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法 JSON 不覆盖旧凭证，错误信息也不回显原始 cookie 内容。"""
    monkeypatch.setattr(settings, "external_sau_path", tmp_path)
    cookie_path = tmp_path / "cookies" / "douyin_acc_31.json"
    raw_secret = '{"cookies": ["super-secret-cookie"], BROKEN}'

    def _write_invalid_cookie() -> None:
        cookie_path.parent.mkdir(parents=True, exist_ok=True)
        cookie_path.write_text(raw_secret, encoding="utf-8")

    _fake_subprocess(
        monkeypatch,
        _FakeProcess(returncode=0, on_communicate=_write_invalid_cookie),
    )
    publisher = SocialAutoUploadPublisher(Platform.DOUYIN)
    credential = {"existing": "credential"}

    assert await publisher.login(31, credential) is False
    assert credential == {"existing": "credential"}
    assert publisher.last_login_error is not None
    assert "合法 JSON" in publisher.last_login_error
    assert "super-secret-cookie" not in publisher.last_login_error


@pytest.mark.asyncio
async def test_login_non_object_cookie_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """语法合法但不是非空对象的文件也不能作为凭证落库。"""
    monkeypatch.setattr(settings, "external_sau_path", tmp_path)
    cookie_path = tmp_path / "cookies" / "douyin_acc_32.json"

    def _write_array_cookie() -> None:
        cookie_path.parent.mkdir(parents=True, exist_ok=True)
        cookie_path.write_text("[]", encoding="utf-8")

    _fake_subprocess(
        monkeypatch,
        _FakeProcess(returncode=0, on_communicate=_write_array_cookie),
    )
    publisher = SocialAutoUploadPublisher(Platform.DOUYIN)
    credential: dict = {}

    assert await publisher.login(32, credential) is False
    assert credential == {}
    assert "非空 JSON 对象" in (publisher.last_login_error or "")


@pytest.mark.asyncio
async def test_login_subprocess_failure_does_not_read_or_mutate_cookie(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """退出码非零时即使磁盘有旧 cookie，也不能把它当成本次登录结果。"""
    monkeypatch.setattr(settings, "external_sau_path", tmp_path)
    cookie_path = tmp_path / "cookies" / "douyin_acc_41.json"
    cookie_path.parent.mkdir(parents=True)
    cookie_path.write_text('{"old": "cookie"}', encoding="utf-8")
    _fake_subprocess(monkeypatch, _FakeProcess(returncode=9))
    publisher = SocialAutoUploadPublisher(Platform.DOUYIN)
    credential = {"existing": "credential"}

    assert await publisher.login(41, credential) is False
    assert credential == {"existing": "credential"}
    assert publisher.last_login_error == "SAU 登录命令失败（退出码 9）"


@pytest.mark.asyncio
async def test_login_spawn_error_returns_sanitized_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SAU 未安装/不可执行时返回稳定错误，不泄露底层异常文本。"""
    monkeypatch.setattr(settings, "external_sau_path", tmp_path)

    async def _raise_spawn_error(*_args, **_kwargs):
        raise OSError("test-only path and secret must stay internal")

    monkeypatch.setattr(
        sau_mod.asyncio,
        "create_subprocess_exec",
        _raise_spawn_error,
    )
    publisher = SocialAutoUploadPublisher(Platform.DOUYIN)
    credential = {"existing": "credential"}

    assert await publisher.login(42, credential) is False
    assert credential == {"existing": "credential"}
    assert publisher.last_login_error == "无法启动或执行 SAU 登录命令"
    assert "secret" not in publisher.last_login_error


def test_reference_marker_never_overwrites_existing_upstream_cookie(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """防止引用 marker 污染已存在、仍由 SAU 自己维护的 cookie 文件。"""
    monkeypatch.setattr(settings, "external_sau_path", tmp_path)
    cookie_path = tmp_path / "cookies" / "douyin_acc_51.json"
    cookie_path.parent.mkdir(parents=True)
    original = '{"cookies": [{"name": "sid", "value": "upstream-owned"}]}'
    cookie_path.write_text(original, encoding="utf-8")
    publisher = SocialAutoUploadPublisher(Platform.DOUYIN)
    marker = publisher._credential_reference(51)

    publisher._sync_cookie_if_needed(51, marker)

    assert cookie_path.read_text(encoding="utf-8") == original


def test_real_credential_sync_uses_current_sau_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无上游文件时，真实 cookie 回写当前 cookies/<platform>_<account>.json。"""
    monkeypatch.setattr(settings, "external_sau_path", tmp_path)
    publisher = SocialAutoUploadPublisher(Platform.DOUYIN)
    credential = {"cookies": [{"name": "sid", "value": "stored"}], "origins": []}

    publisher._sync_cookie_if_needed(61, credential)

    cookie_path = tmp_path / "cookies" / "douyin_acc_61.json"
    assert json.loads(cookie_path.read_text(encoding="utf-8")) == credential
    assert cookie_path.stat().st_mode & 0o777 == 0o600
    assert cookie_path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_publish_uses_current_hyphenated_sau_cli_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the action spelling used by the current upstream sau_cli parser."""
    monkeypatch.setattr(settings, "external_sau_path", tmp_path)
    process = _FakeProcess(returncode=0)
    calls = _fake_subprocess(monkeypatch, process)
    video = tmp_path / "clip.mp4"
    video.touch()
    publisher = SocialAutoUploadPublisher(Platform.DOUYIN)

    result = await publisher._publish_via_cli(
        71,
        PublishContent(
            title="safe test",
            body="",
            content_type=ContentType.VIDEO,
            videos=[str(video)],
        ),
    )

    assert result.success is False
    assert result.outcome_uncertain is True
    assert result.retryable is False
    assert result.raw_response["outcome"] == "unknown"
    serialized = json.dumps(result.raw_response, ensure_ascii=False)
    assert "safe test" not in serialized
    assert "clip.mp4" not in serialized
    assert "cmd" not in result.raw_response
    assert calls[0][0][2:4] == ("douyin", "upload-video")


@pytest.mark.asyncio
async def test_http_url_does_not_hijack_cli_only_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bilibili has no SAU HTTP contract, so a configured URL must not disable its CLI."""
    monkeypatch.setattr(settings, "external_sau_url", "http://127.0.0.1:5409")
    publisher = SocialAutoUploadPublisher(Platform.BILIBILI)
    calls: list[str] = []

    async def fake_cli(*args, **kwargs):
        calls.append("cli")
        return PublishResult(success=False, effect_applied=False, retryable=False)

    async def fake_http(*args, **kwargs):
        calls.append("http")
        raise AssertionError("unsupported HTTP platform selected")

    monkeypatch.setattr(publisher, "_publish_via_cli", fake_cli)
    monkeypatch.setattr(publisher, "_publish_via_http", fake_http)

    await publisher.publish(
        1,
        {},
        PublishContent(title="t", body="", content_type=ContentType.VIDEO),
    )

    assert calls == ["cli"]


def test_external_browser_subprocess_env_does_not_forward_app_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-forward")
    monkeypatch.setenv("FERNET_KEY", "do-not-forward")
    monkeypatch.setenv("API_KEY", "do-not-forward")
    monkeypatch.setenv("PATH", "/safe/path")

    env = sau_mod.build_subprocess_env()

    assert env["PATH"] == "/safe/path"
    assert "OPENAI_API_KEY" not in env
    assert "FERNET_KEY" not in env
    assert "API_KEY" not in env
