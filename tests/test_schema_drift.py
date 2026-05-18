"""tests/test_schema_drift.py — Round 5 schema 漂移自检 + 自动升级单测。

核心契约：
  1. `get_code_alembic_head()` 返回 alembic/versions/ 最新 migration 的 revision
  2. `check_schema_drift()` 在 DB 已 stamp head 时报 in_sync=True
  3. `check_schema_drift()` 在 DB 是 create_all 建的（无 alembic_version 表）时报 in_sync=False
  4. `check_schema_drift()` 在 DB stamp 到 baseline（落后 head）时报 in_sync=False
  5. `try_auto_upgrade()` 已 in_sync 时不真跑（attempted=False, reason="already in sync"）
  6. `try_auto_upgrade(force=True)` 能把 stamp 到 baseline 的旧 DB 升到 head（真实事故重现）

测试模式：与 tests/test_worker_integration.py 一致，用 `SessionLocal.configure(bind=engine)`
+ in-memory file DB（不是 :memory: —— alembic command 是子调用，需要 file DB 真路径），
确保所有 production kwargs（特别是 expire_on_commit=False）生效；测试结束还原 bind。

env 隔离：alembic Python API 走 env.py 的 `_resolve_database_url()`，
优先读 `DATABASE_URL` env —— 用 monkeypatch.setenv 把 env 指到 tmp DB，
不影响默认 settings.database_url 也不污染 ./data/ai_ops.db。
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from ai_ops.core import db as db_mod
from ai_ops.core.models import Base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ensure_alembic_available():
    """alembic 是已声明依赖（pyproject >=1.13），但仍 defensive skip 缺包环境。"""
    try:
        import alembic  # noqa: F401
        from alembic import command  # noqa: F401
        from alembic.config import Config  # noqa: F401
    except ImportError:
        pytest.skip("alembic 未装；本测试需要 alembic Python API")


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """tmp 目录下的 sqlite 文件路径（alembic command 需要真路径，不能 :memory:）。"""
    return tmp_path / "test_schema_drift.db"


@pytest.fixture
def tmp_db_url(tmp_db_path: Path, monkeypatch) -> str:
    """tmp DB 的 sqlalchemy URL，同时 set DATABASE_URL env 让 alembic env.py 接管。

    与 tests/test_alembic_migration.py 的 _alembic_env 套路一致：alembic env.py
    优先读 DATABASE_URL，把它指到 tmp 即可让 alembic command 操作 tmp DB。

    同时 monkeypatch settings.database_url，让 db_mod 里 helper（get_db_alembic_head
    用 settings.database_url 建 engine）也指到 tmp DB —— production helper 路径走通。
    """
    url = f"sqlite:///{tmp_db_path.resolve()}"
    monkeypatch.setenv("DATABASE_URL", url)
    # helper 默认 engine 用 settings.database_url；monkeypatch 之
    monkeypatch.setattr(db_mod.settings, "database_url", url)
    return url


@pytest.fixture
def production_session_on_tmp(tmp_db_url, monkeypatch):
    """与 test_worker_integration.py 同款：rebind 生产 SessionLocal 到 tmp engine。

    返回 (SessionLocal, engine)，调用方按需用。
    """
    engine = create_engine(
        tmp_db_url,
        future=True,
        connect_args={"check_same_thread": False},
    )
    original_bind = db_mod.SessionLocal.kw.get("bind")
    db_mod.SessionLocal.configure(bind=engine)
    try:
        yield db_mod.SessionLocal, engine
    finally:
        db_mod.SessionLocal.configure(bind=original_bind)
        engine.dispose()


def _run_alembic_command(action: str, rev: str | None = None) -> None:
    """跑 alembic command (stamp / upgrade)。

    用与 lifespan 同款的 _alembic_config()，env.py 自然走 DATABASE_URL env
    （由 tmp_db_url fixture 设置）。
    """
    from alembic import command

    cfg = db_mod._alembic_config()
    assert cfg is not None
    if action == "stamp":
        assert rev is not None
        command.stamp(cfg, rev)
    elif action == "upgrade":
        command.upgrade(cfg, rev or "head")
    else:
        raise ValueError(f"unknown alembic action: {action}")


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_get_code_head_returns_latest_revision():
    """code_head 必须返回 alembic/versions/ 最新 migration 的 revision id。

    硬钉 7c7c50aecd6a（Round 6: add metrics.source）—— 当前仓库 head。
    若以后加新 migration，本断言会显眼提醒更新（这是 feature 不是 bug）。
    """
    head = db_mod.get_code_alembic_head()
    assert head == "7c7c50aecd6a", (
        f"code head 期望 7c7c50aecd6a (Round 6 metrics.source migration)，实际 {head}；"
        "如新增了 migration 请同步更新本测试"
    )


def test_check_schema_drift_in_sync_when_db_at_head(
    tmp_db_url, tmp_db_path
):
    """DB stamp 到 head + 跑 upgrade head（真建表）后，check_schema_drift 报 in_sync=True。"""
    # 走真 upgrade，建出所有表（含 alembic_version 写入 head）
    _run_alembic_command("upgrade", "head")
    assert tmp_db_path.exists()

    drift = db_mod.check_schema_drift()
    assert drift["in_sync"] is True
    assert drift["db_head"] == "7c7c50aecd6a"
    assert drift["code_head"] == "7c7c50aecd6a"
    assert drift["missing_migrations"] == []


def test_check_schema_drift_detects_no_alembic_version_table(
    production_session_on_tmp, tmp_db_path
):
    """复现 Round 5 P9 事故：DB 是 create_all 建的（无 alembic_version 表）→ in_sync=False。

    抓手：dev 启动期 SCHEMA-CHECK 必须能识别此场景，否则下次 model 加字段
    开发者 git pull 后 uvicorn 启动会直接炸。
    """
    SessionLocal, engine = production_session_on_tmp
    # 走 Base.metadata.create_all（与早期 dev DB 路径一致）
    Base.metadata.create_all(engine)
    assert tmp_db_path.exists()

    # sanity：业务表已建出，但 alembic_version 表不存在
    insp = inspect(engine)
    tables = insp.get_table_names()
    assert "publish_jobs" in tables  # create_all 路径建表正常
    assert "alembic_version" not in tables  # 但没 alembic version 表

    drift = db_mod.check_schema_drift()
    assert drift["db_head"] is None, "create_all 建的 DB 应无 db_head"
    assert drift["code_head"] == "7c7c50aecd6a"
    assert drift["in_sync"] is False, "P9 事故场景必须被识别为漂移"
    # missing 应列出所有 rev（base → head）
    assert drift["missing_migrations"] == ["b09fbf0bf0f0", "7c183c0ba12a", "7c7c50aecd6a"]


def test_check_schema_drift_detects_old_db_stamped_at_baseline(tmp_db_url):
    """DB stamp 到 baseline（落后 head 一个 rev）→ in_sync=False + missing 只剩一条。

    业务含义：模拟"上次部署只升到 baseline，没跟上后续 migration"——
    SCHEMA-CHECK 必须能精确指出待跑的 migration 列表。
    """
    # 只 stamp 到 baseline，不真跑 upgrade（DB 仍空，但 alembic_version 写了 baseline）
    _run_alembic_command("stamp", "b09fbf0bf0f0")

    drift = db_mod.check_schema_drift()
    assert drift["db_head"] == "b09fbf0bf0f0"
    assert drift["code_head"] == "7c7c50aecd6a"
    assert drift["in_sync"] is False
    assert drift["missing_migrations"] == ["7c183c0ba12a", "7c7c50aecd6a"], (
        f"应缺 2 个 migration（superseded + source），实际 {drift['missing_migrations']}"
    )


def test_try_auto_upgrade_does_nothing_when_in_sync(tmp_db_url):
    """已 in_sync 调 try_auto_upgrade → attempted=False, ok=True, reason='already in sync'。

    意义：节省启动时间 + 避免不必要的 alembic 锁；幂等。
    """
    _run_alembic_command("upgrade", "head")
    assert db_mod.check_schema_drift()["in_sync"] is True

    result = db_mod.try_auto_upgrade(force=True)  # 即便 force=True 也不该真跑
    assert result["attempted"] is False
    assert result["ok"] is True
    assert "already in sync" in result["reason"]


def test_try_auto_upgrade_respects_disabled_default(tmp_db_url, monkeypatch):
    """生产默认 auto_upgrade_db=False，try_auto_upgrade（不 force）必须不动 schema。

    抓手：避免应用进程偷偷改生产 schema（绕过运维审批 + 多进程竞争）。
    """
    # 制造漂移：stamp 到 baseline
    _run_alembic_command("stamp", "b09fbf0bf0f0")
    # 显式钉死 settings 默认值
    monkeypatch.setattr(db_mod.settings, "auto_upgrade_db", False)

    result = db_mod.try_auto_upgrade()  # 不 force
    assert result["attempted"] is False
    assert result["ok"] is False  # 漂移没被治愈，所以 ok=False
    assert "disabled" in result["reason"]

    # DB 仍停在 baseline
    assert db_mod.get_db_alembic_head() == "b09fbf0bf0f0"


def test_try_auto_upgrade_promotes_old_db_to_head(
    production_session_on_tmp, tmp_db_url
):
    """真实事故重现 + 闭环验证（Round 5 P9 事故）：

    构造场景：dev DB 是 baseline 时代 create_all 建的（业务表都在，但只跑了 baseline migration，
    缺 7c183c0ba12a 加的 superseded_by_job_id 字段）→ check_schema_drift 报 in_sync=False
    → try_auto_upgrade(force=True) → check_schema_drift 报 in_sync=True，字段被加上。

    这是 Round 5 的核心契约：dev 启动期 SCHEMA-CHECK + auto_upgrade_db=True
    能自动愈合 P9 事故场景。
    """
    SessionLocal, engine = production_session_on_tmp

    # 步骤 1: 模拟 baseline 时代的 dev DB —— 先 create_all 建出 baseline 那批业务表，
    # 但**不含**后续 migration 加的字段。手段：用一个剥掉 superseded_by_job_id 字段的
    # 临时 metadata create_all，再 stamp 到 baseline。
    #
    # 更简便的等价路径：直接走 baseline migration 真 upgrade（不 upgrade head）
    from alembic import command
    cfg = db_mod._alembic_config()
    command.upgrade(cfg, "b09fbf0bf0f0")  # 只跑 baseline，不跑后续

    # sanity：业务表已建，字段还没加（这正是 P9 事故现场）
    from sqlalchemy import inspect as _inspect
    cols = [c["name"] for c in _inspect(engine).get_columns("publish_jobs")]
    assert "superseded_by_job_id" not in cols, "构造失败：baseline 不该有这字段"

    drift_before = db_mod.check_schema_drift()
    assert drift_before["in_sync"] is False
    assert drift_before["db_head"] == "b09fbf0bf0f0"
    assert drift_before["missing_migrations"] == ["7c183c0ba12a", "7c7c50aecd6a"]

    # 步骤 2: 触发自动升级（force=True 绕开 settings.auto_upgrade_db 默认 False）
    result = db_mod.try_auto_upgrade(force=True)
    assert result["attempted"] is True, f"应真跑 upgrade，实际 {result}"
    assert result["ok"] is True, f"upgrade 应成功，实际 error={result['error']}"
    assert result["from_rev"] == "b09fbf0bf0f0"
    assert result["to_rev"] == "7c7c50aecd6a"

    # 步骤 3: 验证 schema 已对齐 + 字段真加上了
    drift_after = db_mod.check_schema_drift()
    assert drift_after["in_sync"] is True
    assert drift_after["db_head"] == "7c7c50aecd6a"
    assert drift_after["missing_migrations"] == []

    cols_after = [c["name"] for c in _inspect(engine).get_columns("publish_jobs")]
    assert "superseded_by_job_id" in cols_after, "Round 1 superseded 字段必须被自动升级加上"
    # Round 6 新加：metrics.source 字段也应被升级路径自动加上
    metrics_cols_after = [c["name"] for c in _inspect(engine).get_columns("metrics")]
    assert "source" in metrics_cols_after, "Round 6 metrics.source 字段必须被自动升级加上"


def test_try_auto_upgrade_dry_run_does_not_change_db(tmp_db_url):
    """dry_run=True 应只返回 "会做什么" 不真改 schema —— 便于运维预演。"""
    _run_alembic_command("stamp", "b09fbf0bf0f0")

    result = db_mod.try_auto_upgrade(dry_run=True, force=True)
    assert result["attempted"] is False
    assert result["ok"] is True
    assert "dry_run" in result["reason"]
    assert result["from_rev"] == "b09fbf0bf0f0"
    assert result["to_rev"] == "7c7c50aecd6a"

    # DB 仍停在 baseline，未被改动
    assert db_mod.get_db_alembic_head() == "b09fbf0bf0f0"
