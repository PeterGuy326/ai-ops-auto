"""命令行入口 — typer 驱动。"""
from __future__ import annotations

import typer

from .config import settings
from .core.db import init_db as _init_db

app = typer.Typer(help="ai-ops-auto CLI")


@app.command("init-db")
def cmd_init_db():
    """创建所有表。"""
    _init_db()
    typer.echo(f"OK: db initialized at {settings.database_url}")


@app.command("serve")
def cmd_serve(host: str = "127.0.0.1", port: int = 8000):
    """启动 API。"""
    import uvicorn

    uvicorn.run("ai_ops.api.main:app", host=host, port=port, reload=False)


@app.command("gen-fernet-key")
def cmd_gen_fernet_key():
    """生成一个 Fernet key（粘到 .env 的 FERNET_KEY）。"""
    from cryptography.fernet import Fernet

    typer.echo(Fernet.generate_key().decode())


if __name__ == "__main__":
    app()
