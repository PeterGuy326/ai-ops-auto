"""命令行入口 — typer 驱动。"""

from __future__ import annotations

import json
import ipaddress
from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import SettingsError
import typer

from .agent_contract.cli_commands import agent_app
from .jobhunt.cli_commands import jobhunt_app
from .reports.cli_commands import report_app

app = typer.Typer(
    help="ai-ops-auto CLI",
    # A command bug must never make Rich render Settings internals and their
    # credential-bearing local variables. Configuration errors also have a
    # dedicated redacted process boundary in :func:`main` below.
    pretty_exceptions_show_locals=False,
)
_DEMO_VERSION = "offline-demo-v1"


class _LazySettings:
    """Delay Settings validation until a command actually needs runtime config."""

    def __getattr__(self, name: str):
        from .config import settings as configured

        return getattr(configured, name)


# Kept as a module seam for command tests and embedding callers. Importing the
# CLI remains safe when configuration is invalid, which lets doctor report a
# structured, redacted error instead of failing before Typer starts.
settings = _LazySettings()


def _is_loopback_bind_host(value: str) -> bool:
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _init_db():
    from .core.db import init_db

    return init_db()


def _echo_json(payload: object) -> None:
    """Write one machine-readable JSON document to stdout."""
    typer.echo(json.dumps(payload, ensure_ascii=False, sort_keys=True))


@app.command("init-db")
def cmd_init_db():
    """安全初始化或升级数据库到 Alembic head。"""
    try:
        _init_db()
    except Exception as exc:
        # 错误信息只描述 schema 状态；数据库 URL（可能含密码）绝不输出。
        typer.echo(
            "ERROR: database initialization refused or failed "
            f"({type(exc).__name__}); database URL was not logged",
            err=True,
        )
        raise typer.Exit(code=1) from exc
    # DATABASE_URL may embed a username/password.  Logs need only the result,
    # never the connection string.
    typer.echo("OK: db initialized")


@app.command("doctor")
def cmd_doctor(
    as_json: bool = typer.Option(
        False,
        "--json",
        help="输出稳定的 JSON 检查结果，便于 Agent 和脚本消费。",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help="把可选能力的 WARN 也视为非零退出。",
    ),
):
    """只读检查数据库、资源、安全配置、调度器和外部能力。"""
    try:
        from .doctor import run_doctor

        report = run_doctor()
    except Exception as exc:
        # Doctor itself is a trust boundary: never echo arbitrary exception text,
        # because database drivers may include a credential-bearing endpoint.
        invalid_config = type(exc).__name__ in {"ValidationError", "SettingsError"}
        code = "invalid_configuration" if invalid_config else "doctor_failed"
        message = (
            "配置校验失败；请检查环境变量和 .env"
            if invalid_config
            else f"诊断器未能完成（{type(exc).__name__}）"
        )
        if as_json:
            _echo_json(
                {
                    "error": {"code": code, "message": message},
                    "exit_code": 1,
                    "ok": False,
                    "schema_version": 1,
                    "strict": strict,
                }
            )
        else:
            typer.echo(f"ERROR: {message}", err=True)
        raise typer.Exit(code=1) from None

    if as_json:
        _echo_json(report.to_dict(strict=strict))
    else:
        typer.echo(report.render_human(strict=strict))
    exit_code = report.exit_code_for(strict=strict)
    if exit_code:
        raise typer.Exit(code=exit_code)


@app.command("demo")
def cmd_demo(
    as_json: bool = typer.Option(
        False,
        "--json",
        help="输出稳定的 JSON 结果，便于 Agent 和脚本消费。",
    ),
    database: Path | None = typer.Option(
        None,
        "--database",
        dir_okay=False,
        resolve_path=False,
        help="新建的隔离 SQLite 文件；必须尚不存在。默认使用临时文件。",
    ),
    keep_data: bool | None = typer.Option(
        None,
        "--keep-data/--no-keep-data",
        help="保留或清理演示库；默认保留显式路径、清理临时路径。",
    ),
):
    """运行零凭证、零外部调用的完整离线价值闭环。"""
    import asyncio

    try:
        from .demo import run_offline_demo

        summary = asyncio.run(
            run_offline_demo(
                database_path=database,
                keep_data=keep_data,
            )
        )
    except FileExistsError:
        message = "演示数据库已存在；请选择一个尚不存在的路径"
        if as_json:
            _echo_json(
                {
                    "demo_version": _DEMO_VERSION,
                    "error": {"code": "database_exists", "message": message},
                    "exit_code": 1,
                    "ok": False,
                }
            )
        else:
            typer.echo(f"ERROR: {message}", err=True)
        raise typer.Exit(code=1) from None
    except Exception as exc:
        # The exception text may contain a local path or adapter detail. Keep the
        # public error deterministic and redacted; operators can rerun tests for
        # a traceback when developing the project.
        message = f"离线演示失败（{type(exc).__name__}）"
        if as_json:
            _echo_json(
                {
                    "demo_version": _DEMO_VERSION,
                    "error": {"code": "demo_failed", "message": message},
                    "exit_code": 1,
                    "ok": False,
                }
            )
        else:
            typer.echo(f"ERROR: {message}", err=True)
        raise typer.Exit(code=1) from None

    if as_json:
        _echo_json(summary.model_dump(mode="json"))
    else:
        typer.echo(summary.to_human_text())
    if summary.exit_code:
        raise typer.Exit(code=summary.exit_code)


@app.command("serve")
def cmd_serve(
    host: str | None = typer.Option(None, help="监听地址；默认读取 API_HOST。"),
    port: int | None = typer.Option(None, min=1, max=65535, help="端口；默认读取 API_PORT。"),
    log_level: str | None = typer.Option(None, help="日志级别；默认读取 API_LOG_LEVEL。"),
):
    """启动控制面 API（后台调度请另跑 ``ai-ops worker``）。"""
    import uvicorn

    from .api.auth import api_key_dev_mode

    effective_host = host or settings.api_host
    if api_key_dev_mode() and not _is_loopback_bind_host(effective_host):
        typer.echo(
            "ERROR: LEGACY_DEV_AUTH_BYPASS may only bind to a loopback host",
            err=True,
        )
        raise typer.Exit(code=1)
    uvicorn.run(
        "ai_ops.api.main:app",
        host=effective_host,
        port=port or settings.api_port,
        log_level=log_level or settings.api_log_level,
        reload=False,
    )


@app.command("worker")
def cmd_worker(
    poll_seconds: float | None = typer.Option(
        None,
        min=0.1,
        help="持久任务扫描间隔；默认读取 SCHEDULER_POLL_SECONDS。",
    ),
    batch_size: int = typer.Option(
        100,
        min=1,
        max=1000,
        help="每轮最多扫描的 PublishJob 数。",
    ),
):
    """启动唯一的调度/恢复 worker；生产环境与 API 分进程运行。"""
    import asyncio

    from .scheduler.service import run_scheduler_service

    if not settings.auto_publish_enabled:
        typer.echo(
            "安全模式：AUTO_PUBLISH_ENABLED=false，worker 不会执行发布任务；"
            "周期健康检查和报表仍会运行。"
        )
    try:
        asyncio.run(
            run_scheduler_service(
                poll_seconds=poll_seconds,
                limit=batch_size,
            )
        )
    except KeyboardInterrupt:
        typer.echo("worker stopped")


@app.command("gen-fernet-key")
def cmd_gen_fernet_key():
    """生成一个 Fernet key（粘到 .env 的 FERNET_KEY）。"""
    from cryptography.fernet import Fernet

    typer.echo(Fernet.generate_key().decode())


@app.command("gen-principal-token")
def cmd_gen_principal_token():
    """生成一次性 Bearer token 及其 AGENT_PRINCIPALS SHA-256 verifier。"""
    import hashlib
    import secrets

    token = "aop_" + secrets.token_urlsafe(32)
    _echo_json(
        {
            "schema_version": 1,
            "token": token,
            "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        }
    )


@app.command("zhihu-login")
def cmd_zhihu_login(account_id: int = typer.Argument(..., min=1)):
    """为一个知乎账号建立隔离的 CLI 扫码登录态。"""
    import asyncio

    from .core.db import session_scope
    from .core.enums import Platform
    from .core.models import Account
    from .publishers.zhihu_cli import ZhihuCliPublisher

    with session_scope() as session:
        account = session.get(Account, account_id)
        if account is None:
            typer.echo(f"ERROR: account {account_id} 不存在", err=True)
            raise typer.Exit(code=1)
        if Platform(account.platform) != Platform.ZHIHU:
            typer.echo(f"ERROR: account {account_id} 不是知乎账号", err=True)
            raise typer.Exit(code=1)

    publisher = ZhihuCliPublisher()
    typer.echo("将启动第三方 zhihu-cli 的二维码登录；cookie 只保存在该 account_id 的隔离目录。")
    ok = asyncio.run(publisher.login_interactive(account_id))
    if not ok:
        typer.echo(f"ERROR: {publisher.last_login_error or '知乎 CLI 登录失败'}", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"OK: account {account_id} 的知乎 CLI 登录态已在线验证")
    if publisher.last_external_account_id is None:
        typer.echo(
            "WARNING: whoami 未返回稳定 id；该账号暂不能用于 Agent exact 发布。",
            err=True,
        )
        return
    typer.echo(f"知乎稳定账号标识: {publisher.last_external_account_id}")
    typer.echo(
        "请通过 PATCH /accounts/"
        f'{account_id} 提交 {{"external_account_id":"{publisher.last_external_account_id}"}}；'
        "服务会将其保存到 Account.profile，本命令不会自动修改数据库。"
    )


# 数据回流自动出报子组：`python -m ai_ops.cli report daily/weekly`
app.add_typer(report_app, name="report")

# 求职投递专题：`python -m ai_ops.cli jobhunt parse-resume ...`
app.add_typer(jobhunt_app, name="jobhunt")

# Agent-native HTTP 契约：只访问控制面，不直连数据库。
app.add_typer(agent_app, name="agent")


def main() -> None:
    """Console boundary that redacts configuration validation failures."""

    try:
        app()
    except (ValidationError, SettingsError):
        typer.echo(
            "ERROR: configuration validation failed; secret values were not logged",
            err=True,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
