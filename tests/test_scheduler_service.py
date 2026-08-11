from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

from typer.testing import CliRunner

from ai_ops import cli
from ai_ops.scheduler import service


async def test_scheduler_service_owns_queue_and_always_shuts_down(monkeypatch):
    calls: list[object] = []

    monkeypatch.setattr(service, "init_observability", lambda: calls.append("observability"))
    monkeypatch.setattr(
        service,
        "require_database_at_head",
        lambda: calls.append("schema-head"),
    )
    monkeypatch.setattr(service.queue, "start", lambda: calls.append("queue-start"))
    monkeypatch.setattr(service.queue, "shutdown", lambda: calls.append("queue-stop"))
    monkeypatch.setattr(service, "_register_periodic_jobs", lambda: calls.append("crons"))

    async def fake_loop(**kwargs):
        calls.append(("loop", kwargs["poll_seconds"], kwargs["limit"]))
        raise RuntimeError("stop test loop")

    monkeypatch.setattr(service, "run_worker_loop", fake_loop)

    try:
        await service.run_scheduler_service(poll_seconds=0.5, limit=7)
    except RuntimeError as exc:
        assert str(exc) == "stop test loop"

    assert calls == [
        "observability",
        "schema-head",
        "queue-start",
        "crons",
        ("loop", 0.5, 7),
        "queue-stop",
    ]


def test_worker_cli_passes_runtime_options(monkeypatch):
    seen: dict[str, object] = {}

    async def fake_service(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(service, "run_scheduler_service", fake_service)
    monkeypatch.setattr(cli.settings, "auto_publish_enabled", True)

    result = CliRunner().invoke(
        cli.app,
        ["worker", "--poll-seconds", "2.5", "--batch-size", "17"],
    )

    assert result.exit_code == 0, result.output
    assert seen == {"poll_seconds": 2.5, "limit": 17}


def test_worker_cli_explains_safe_mode(monkeypatch):
    async def fake_service(**kwargs):
        await asyncio.sleep(0)

    monkeypatch.setattr(service, "run_scheduler_service", fake_service)
    monkeypatch.setattr(cli.settings, "auto_publish_enabled", False)

    result = CliRunner().invoke(cli.app, ["worker"])

    assert result.exit_code == 0, result.output
    assert "AUTO_PUBLISH_ENABLED=false" in result.output
    assert "指标读取" in result.output
    assert "外部平台" in result.output
    assert "请停止 worker" in result.output


def test_init_db_cli_never_prints_database_credentials(monkeypatch):
    sentinel = "db-password-must-not-leak"
    monkeypatch.setattr(
        cli.settings,
        "database_url",
        f"postgresql://user:{sentinel}@localhost/ai_ops",
    )
    monkeypatch.setattr(cli, "_init_db", lambda: None)

    result = CliRunner().invoke(cli.app, ["init-db"])

    assert result.exit_code == 0
    assert result.output.strip() == "OK: db initialized"
    assert sentinel not in result.output


def test_serve_cli_reads_api_settings_and_allows_explicit_override(monkeypatch):
    import uvicorn

    calls: list[dict] = []
    monkeypatch.setattr(
        uvicorn, "run", lambda *args, **kwargs: calls.append({"args": args, **kwargs})
    )
    monkeypatch.setattr(cli.settings, "api_host", "127.0.0.9")
    monkeypatch.setattr(cli.settings, "api_port", 8123)
    monkeypatch.setattr(cli.settings, "api_log_level", "warning")

    default_result = CliRunner().invoke(cli.app, ["serve"])
    override_result = CliRunner().invoke(
        cli.app,
        ["serve", "--host", "0.0.0.0", "--port", "9001", "--log-level", "debug"],
    )

    assert default_result.exit_code == 0, default_result.output
    assert override_result.exit_code == 0, override_result.output
    assert calls[0] == {
        "args": ("ai_ops.api.main:app",),
        "host": "127.0.0.9",
        "port": 8123,
        "log_level": "warning",
        "reload": False,
    }
    assert calls[1]["host"] == "0.0.0.0"
    assert calls[1]["port"] == 9001
    assert calls[1]["log_level"] == "debug"


def test_zhihu_login_cli_uses_explicit_qrcode_flow(monkeypatch):
    from ai_ops.core import db as db_mod
    from ai_ops.core.enums import Platform
    from ai_ops.publishers.zhihu_cli import ZhihuCliPublisher

    account = SimpleNamespace(
        platform=Platform.ZHIHU,
        profile={"external_account_id": "zhihu:id:old-value"},
    )

    @contextmanager
    def fake_scope():
        yield SimpleNamespace(get=lambda model, account_id: account)

    calls: list[int] = []

    async def fake_login(self, account_id):
        calls.append(account_id)
        self.last_external_account_id = "zhihu:id:person-id"
        return True

    monkeypatch.setattr(db_mod, "session_scope", fake_scope)
    monkeypatch.setattr(ZhihuCliPublisher, "login_interactive", fake_login)

    result = CliRunner().invoke(cli.app, ["zhihu-login", "17"])

    assert result.exit_code == 0, result.output
    assert calls == [17]
    assert "在线验证" in result.output
    assert "zhihu:id:person-id" in result.output
    assert 'PATCH /accounts/17 提交 {"external_account_id":"zhihu:id:person-id"}' in result.output
    assert "保存到 Account.profile" in result.output
    assert "不会自动修改数据库" in result.output
    assert account.profile == {"external_account_id": "zhihu:id:old-value"}
