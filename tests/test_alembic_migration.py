"""tests/test_alembic_migration.py — alembic 迁移管理基础设施单测。

核心契约：
  1. 空 DB 上 `alembic upgrade head` 必须成功（生产首次部署链路）
  2. upgrade head 后 publish_jobs.superseded_by_job_id 字段存在（首次 migration 真生效）
  3. `alembic downgrade base` 必须成功，且对称（schema 可回滚）

为什么不走 SessionLocal.configure(bind=engine) 套路：
  alembic CLI 是子进程，本身就跑独立 engine + DATABASE_URL env，测试侧用
  subprocess.run 调 alembic 命令最贴近生产路径——也避免污染 SessionLocal 全局态。

为什么用 tmpdir：
  不污染默认 ./data/ai_ops.db，不影响并行测试。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


# 项目根（pyproject.toml 所在）
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _alembic_env(db_path: Path) -> dict:
    """构造跑 alembic CLI 的 env：DATABASE_URL 指向 tmp DB（绝对路径 4 个斜杠）。"""
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{db_path.resolve()}"
    return env


def _run_alembic(args: list[str], db_path: Path) -> subprocess.CompletedProcess:
    """跑 alembic CLI；cwd=项目根（alembic.ini 必须在 cwd），返回 CompletedProcess。"""
    return subprocess.run(
        ["alembic", *args],
        cwd=str(_REPO_ROOT),
        env=_alembic_env(db_path),
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """每个用例独立的空 sqlite DB 文件，测试结束自动清理。"""
    db = tmp_path / "test_migration.db"
    if db.exists():
        db.unlink()
    yield db
    if db.exists():
        db.unlink()


@pytest.fixture(autouse=True)
def _ensure_alembic_available():
    """如果当前环境没装 alembic CLI（如最小 pip 环境），整文件 skip 而非误报失败。"""
    if shutil.which("alembic") is None:
        pytest.skip("alembic CLI 未安装；本测试需要 alembic 可执行文件在 PATH")


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_alembic_upgrade_head_on_empty_db(tmp_db: Path) -> None:
    """空 DB 上 alembic upgrade head 必须 returncode=0 且建出所有业务表。

    这是生产首次部署的核心链路：新机器没有 DB，alembic upgrade head 一键到位。
    任何 traceback / non-zero exit = 部署链路坏了 = P0。
    """
    result = _run_alembic(["upgrade", "head"], tmp_db)
    assert result.returncode == 0, (
        f"alembic upgrade head 失败:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    # DB 文件已生成
    assert tmp_db.exists(), "upgrade head 后 sqlite 文件应存在"

    # 业务核心表必须都有（baseline migration 真生效）
    with sqlite3.connect(str(tmp_db)) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    expected = {"topics", "accounts", "articles", "assets", "publish_jobs",
                "metrics", "alembic_version"}
    missing = expected - tables
    assert not missing, f"upgrade head 后缺表: {missing} (实际有: {sorted(tables)})"


def test_publish_job_has_superseded_by_column_after_migration(tmp_db: Path) -> None:
    """upgrade head 后 publish_jobs 表必须包含 superseded_by_job_id 列。

    这是首个真正的 schema 变更（7c183c0ba12a）是否生效的硬证据。
    """
    result = _run_alembic(["upgrade", "head"], tmp_db)
    assert result.returncode == 0, f"upgrade 失败: {result.stderr}"

    with sqlite3.connect(str(tmp_db)) as conn:
        cols = [row[1] for row in conn.execute("PRAGMA table_info('publish_jobs')")]

    assert "superseded_by_job_id" in cols, (
        f"publish_jobs 应有 superseded_by_job_id 列，实际列: {cols}"
    )


def test_alembic_downgrade_base_removes_schema(tmp_db: Path) -> None:
    """先 upgrade head 再 downgrade base，业务表必须全部清空（schema 可回滚）。

    downgrade 路径在 dev / staging 调试时常用：改坏了往回退一步。
    如果 downgrade 不工作 = 单向迁移 = 不可用于生产。
    """
    # 先升到 head
    up = _run_alembic(["upgrade", "head"], tmp_db)
    assert up.returncode == 0, f"upgrade 失败: {up.stderr}"

    # 再降到 base
    down = _run_alembic(["downgrade", "base"], tmp_db)
    assert down.returncode == 0, (
        f"alembic downgrade base 失败:\nstdout={down.stdout}\nstderr={down.stderr}"
    )

    # 业务表应全删，只剩 alembic_version
    with sqlite3.connect(str(tmp_db)) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    business_tables = {"topics", "accounts", "articles", "assets", "publish_jobs", "metrics"}
    leftover = business_tables & tables
    assert not leftover, (
        f"downgrade base 后业务表应清空，但残留: {leftover}（全部表: {sorted(tables)}）"
    )
