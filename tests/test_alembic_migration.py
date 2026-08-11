"""tests/test_alembic_migration.py — alembic 迁移管理基础设施单测。

核心契约：
  1. 空 DB 上 `alembic upgrade head` 必须成功（生产首次部署链路）
  2. upgrade head 后 publish_jobs.superseded_by_job_id 字段存在（首次 migration 真生效）
  3. Agent 契约计划/审批/幂等表和 job 唯一约束存在
  4. 存量 PublishJob 升级后 plan_id=NULL，不改变历史语义
  5. 存量 Asset 升级保持可读，vault metadata 完整性约束和计划快照生效
  6. exact job 的审批时间从可变重试时间中拆出并正确回填，legacy job 保持 NULL
  7. Agent 契约降级后移除新增 schema，同时保留旧 Asset 数据
  8. `alembic downgrade base` 必须成功，且对称（schema 可回滚）

为什么不走 SessionLocal.configure(bind=engine) 套路：
  alembic CLI 是子进程，本身就跑独立 engine + DATABASE_URL env，测试侧用
  subprocess.run 调 alembic 命令最贴近生产路径——也避免污染 SessionLocal 全局态。

为什么用 tmpdir：
  不污染默认 ./data/ai_ops.db，不影响并行测试。
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
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
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    expected = {
        "topics",
        "accounts",
        "articles",
        "assets",
        "publish_jobs",
        "metrics",
        "publication_plans",
        "approval_requests",
        "agent_operations",
        "alembic_version",
    }
    missing = expected - tables
    assert not missing, f"upgrade head 后缺表: {missing} (实际有: {sorted(tables)})"


def test_alembic_never_falls_back_to_local_sqlite_after_invalid_settings(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env["SCHEDULER_BACKEND"] = "invalid-backend"
    fallback_db = tmp_path / "data" / "ai_ops.db"

    result = subprocess.run(
        [
            "alembic",
            "-c",
            str(_REPO_ROOT / "alembic.ini"),
            "upgrade",
            "head",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode != 0
    assert not fallback_db.exists()


@pytest.mark.parametrize("database_url", ["", " \t "])
def test_alembic_rejects_explicit_blank_database_url(
    tmp_path: Path,
    database_url: str,
) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    fallback_db = tmp_path / "data" / "ai_ops.db"

    result = subprocess.run(
        [
            "alembic",
            "-c",
            str(_REPO_ROOT / "alembic.ini"),
            "upgrade",
            "head",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode != 0
    assert "DATABASE_URL is set but empty" in result.stderr
    assert not fallback_db.exists()


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


def _unique_column_sets(conn: sqlite3.Connection, table: str) -> set[tuple[str, ...]]:
    """返回 SQLite 表上所有 unique index 的有序列集合。"""
    unique_indexes = [row[1] for row in conn.execute(f"PRAGMA index_list('{table}')") if row[2]]
    return {
        tuple(row[2] for row in conn.execute(f"PRAGMA index_info('{name}')"))
        for name in unique_indexes
    }


def _insert_legacy_job(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """只用 f1c3b8a7d2e4 已存在的列构造一条存量 job。"""
    now = "2026-08-11 00:00:00"
    topic_id = conn.execute(
        """
        INSERT INTO topics
            (name, category, keywords, persona, target_platforms, heat_score,
             notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("contract migration", "test", "[]", "{}", '["zhihu"]', 0.0, "", now),
    ).lastrowid
    account_id = conn.execute(
        """
        INSERT INTO accounts
            (platform, nickname, profile, topic_id, encrypted_credential, health,
             risk_level, daily_quota, last_publish_at, last_health_check_at,
             created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("zhihu", "legacy account", "{}", topic_id, b"", "healthy", 0, 1, None, None, now),
    ).lastrowid
    article_id = conn.execute(
        """
        INSERT INTO articles
            (topic_id, title, body, content_type, status, target_platforms,
             target_account_ids, scheduled_at, extra, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            topic_id,
            "legacy article",
            "body",
            "long_article",
            "scheduled",
            '["zhihu"]',
            "[]",
            None,
            "{}",
            now,
            now,
        ),
    ).lastrowid
    job_id = conn.execute(
        """
        INSERT INTO publish_jobs
            (article_id, account_id, platform, status, publisher_kind, attempts,
             max_attempts, platform_post_id, platform_url, error, raw_response,
             scheduled_at, started_at, finished_at, created_at,
             superseded_by_job_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            article_id,
            account_id,
            "zhihu",
            "pending",
            "",
            0,
            3,
            None,
            None,
            None,
            "{}",
            None,
            None,
            None,
            now,
            None,
        ),
    ).lastrowid
    conn.commit()
    assert topic_id is not None and account_id is not None
    assert article_id is not None and job_id is not None
    return int(article_id), int(account_id), int(job_id)


def _insert_legacy_asset(conn: sqlite3.Connection, article_id: int) -> int:
    """写入一条契约迁移前的 Asset，验证 additive upgrade/downgrade。"""

    asset_id = conn.execute(
        """
        INSERT INTO assets
            (article_id, asset_type, source, local_path, meta, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            article_id,
            "image",
            "user_upload",
            "legacy/cover.png",
            '{"role":"cover"}',
            "2026-08-11 00:00:00",
        ),
    ).lastrowid
    conn.commit()
    assert asset_id is not None
    return int(asset_id)


def test_agent_contract_schema_and_constraints_after_migration(tmp_db: Path) -> None:
    result = _run_alembic(["upgrade", "head"], tmp_db)
    assert result.returncode == 0, f"upgrade 失败: {result.stderr}"

    with sqlite3.connect(str(tmp_db)) as conn:
        plan_cols = {row[1]: row for row in conn.execute("PRAGMA table_info('publication_plans')")}
        approval_cols = {
            row[1]: row for row in conn.execute("PRAGMA table_info('approval_requests')")
        }
        operation_cols = {
            row[1]: row for row in conn.execute("PRAGMA table_info('agent_operations')")
        }
        metric_cols = {row[1]: row for row in conn.execute("PRAGMA table_info('metrics')")}
        job_cols = {row[1]: row for row in conn.execute("PRAGMA table_info('publish_jobs')")}
        asset_cols = {row[1]: row for row in conn.execute("PRAGMA table_info('assets')")}

        assert {
            "article_id",
            "state",
            "content_digest",
            "plan_digest",
            "content_snapshot",
            "targets",
            "planned_for",
            "created_by",
            "created_by_type",
            "created_at",
            "updated_at",
        } <= set(plan_cols)
        assert plan_cols["planned_for"][3] == 1, "planned_for 必须绑定审批时间"
        assert plan_cols["content_snapshot"][3] == 1
        assert {"content_sha256", "size_bytes", "storage_kind"} <= set(asset_cols)
        assert {
            "plan_id",
            "plan_digest",
            "status",
            "requested_by",
            "requested_by_type",
            "requested_at",
            "decided_by",
            "decided_by_type",
            "decided_at",
            "decision_reason",
            "expires_at",
            "updated_at",
        } <= set(approval_cols)
        assert {
            "principal_id",
            "principal_type",
            "operation",
            "idempotency_key",
            "request_digest",
            "response_status_code",
            "response_json",
            "lease_token",
            "lease_expires_at",
            "created_at",
            "updated_at",
        } <= set(operation_cols)
        assert operation_cols["response_status_code"][3] == 0
        assert operation_cols["response_json"][3] == 0
        assert "agent_operation_id" in metric_cols
        assert metric_cols["agent_operation_id"][3] == 0
        assert "plan_id" in job_cols and job_cols["plan_id"][3] == 0
        assert "approved_planned_for" in job_cols
        assert job_cols["approved_planned_for"][3] == 0

        assert (
            "principal_id",
            "operation",
            "idempotency_key",
        ) in _unique_column_sets(conn, "agent_operations")
        assert ("plan_id", "account_id") in _unique_column_sets(conn, "publish_jobs")
        assert ("agent_operation_id",) in _unique_column_sets(conn, "metrics")

        job_foreign_keys = {
            (row[3], row[2], row[4])
            for row in conn.execute("PRAGMA foreign_key_list('publish_jobs')")
        }
        assert ("plan_id", "publication_plans", "id") in job_foreign_keys
        metric_foreign_keys = {
            (row[3], row[2], row[4]) for row in conn.execute("PRAGMA foreign_key_list('metrics')")
        }
        assert ("agent_operation_id", "agent_operations", "id") in metric_foreign_keys


def test_legacy_job_upgrade_and_contract_idempotency_constraints(tmp_db: Path) -> None:
    before = _run_alembic(["upgrade", "f1c3b8a7d2e4"], tmp_db)
    assert before.returncode == 0, f"契约前迁移失败: {before.stderr}"
    with sqlite3.connect(str(tmp_db)) as conn:
        article_id, account_id, legacy_job_id = _insert_legacy_job(conn)

    upgraded = _run_alembic(["upgrade", "head"], tmp_db)
    assert upgraded.returncode == 0, f"契约迁移失败: {upgraded.stderr}"

    now = "2026-08-11 00:00:00"
    digest = "a" * 64
    targets = json.dumps([{"account_id": account_id, "platform": "zhihu"}])
    with sqlite3.connect(str(tmp_db)) as conn:
        legacy_plan_id = conn.execute(
            "SELECT plan_id FROM publish_jobs WHERE id = ?",
            (legacy_job_id,),
        ).fetchone()[0]
        assert legacy_plan_id is None

        plan_id = conn.execute(
            """
            INSERT INTO publication_plans
                (article_id, state, content_digest, plan_digest, content_snapshot,
                 targets, planned_for, created_by, created_by_type, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article_id,
                "approved",
                digest,
                digest,
                "{}",
                targets,
                now,
                "agent-a",
                "agent",
                now,
                now,
            ),
        ).lastrowid
        assert plan_id is not None

        conn.execute(
            """
            INSERT INTO agent_operations
                (principal_id, principal_type, operation, idempotency_key,
                 request_digest, response_status_code, response_json,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            ("agent-a", "agent", "schedule", "request-1", digest, now, now),
        )
        conn.commit()
        claimed = conn.execute(
            """
            SELECT response_status_code, response_json FROM agent_operations
            WHERE principal_id = 'agent-a' AND operation = 'schedule'
            """
        ).fetchone()
        assert claimed == (None, None)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO agent_operations
                    (principal_id, principal_type, operation, idempotency_key,
                     request_digest, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                ("agent-a", "agent", "schedule", "request-1", digest, now, now),
            )
        conn.rollback()

        for key, status_code, response_json in (
            ("half-status", 200, None),
            ("half-response", None, "{}"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO agent_operations
                        (principal_id, principal_type, operation, idempotency_key,
                         request_digest, response_status_code, response_json,
                         created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "agent-a",
                        "agent",
                        "schedule",
                        key,
                        digest,
                        status_code,
                        response_json,
                        now,
                        now,
                    ),
                )
            conn.rollback()

        for key, lease_token, lease_expires_at, status_code, response_json in (
            ("lease-token-only", "b" * 64, None, None, None),
            ("lease-expiry-only", None, now, None, None),
            ("lease-short-token", "short", now, None, None),
            ("completed-still-leased", "b" * 64, now, 200, "{}"),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO agent_operations
                        (principal_id, principal_type, operation, idempotency_key,
                         request_digest, response_status_code, response_json,
                         lease_token, lease_expires_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "agent-a",
                        "agent",
                        "collect_metrics",
                        key,
                        digest,
                        status_code,
                        response_json,
                        lease_token,
                        lease_expires_at,
                        now,
                        now,
                    ),
                )
            conn.rollback()

        metrics_operation_id = conn.execute(
            """
            INSERT INTO agent_operations
                (principal_id, principal_type, operation, idempotency_key,
                 request_digest, lease_token, lease_expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "agent-a",
                "agent",
                "collect_metrics",
                "valid-metrics-lease",
                digest,
                "c" * 64,
                now,
                now,
                now,
            ),
        ).lastrowid
        assert metrics_operation_id is not None

        # 存量 NULL plan_id 不参与冲突；同 plan/账号的契约 job 只能建一条。
        publish_values = (
            article_id,
            plan_id,
            account_id,
            "zhihu",
            "pending",
            "",
            0,
            3,
            "{}",
            now,
            now,
        )
        contract_job_id = conn.execute(
            """
            INSERT INTO publish_jobs
                (article_id, plan_id, account_id, platform, status,
                 publisher_kind, attempts, max_attempts, raw_response,
                 approved_planned_for, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            publish_values,
        ).lastrowid
        assert contract_job_id is not None
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO publish_jobs
                    (article_id, plan_id, account_id, platform, status,
                     publisher_kind, attempts, max_attempts, raw_response,
                     approved_planned_for, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                publish_values,
            )

        metric_values = (
            contract_job_id,
            metrics_operation_id,
            now,
            1,
            2,
            3,
            4,
            "{}",
            "manual",
        )
        conn.execute(
            """
            INSERT INTO metrics
                (job_id, agent_operation_id, collected_at, likes, comments,
                 shares, views, raw, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            metric_values,
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO metrics
                    (job_id, agent_operation_id, collected_at, likes, comments,
                     shares, views, raw, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                metric_values,
            )


def test_contract_job_planned_time_is_backfilled_and_legacy_jobs_stay_compatible(
    tmp_db: Path,
) -> None:
    before = _run_alembic(["upgrade", "a2f7c9e4d1b6"], tmp_db)
    assert before.returncode == 0, f"契约迁移失败: {before.stderr}"

    now = "2026-08-11 00:00:00"
    digest = "e" * 64
    with sqlite3.connect(str(tmp_db)) as conn:
        article_id, account_id, legacy_job_id = _insert_legacy_job(conn)
        plan_id = conn.execute(
            """
            INSERT INTO publication_plans
                (article_id, state, content_digest, plan_digest, content_snapshot,
                 targets, planned_for, created_by, created_by_type, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article_id,
                "scheduled",
                digest,
                digest,
                "{}",
                "[]",
                now,
                "agent-a",
                "agent",
                now,
                now,
            ),
        ).lastrowid
        exact_job_id = conn.execute(
            """
            INSERT INTO publish_jobs
                (article_id, plan_id, account_id, platform, status,
                 publisher_kind, attempts, max_attempts, raw_response,
                 scheduled_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article_id,
                plan_id,
                account_id,
                "zhihu",
                "retrying",
                "zhihu_cli",
                1,
                3,
                "{}",
                "2026-08-11 00:05:00",
                now,
            ),
        ).lastrowid
        conn.commit()

    upgraded = _run_alembic(["upgrade", "head"], tmp_db)
    assert upgraded.returncode == 0, f"planned time migration 失败: {upgraded.stderr}"

    with sqlite3.connect(str(tmp_db)) as conn:
        rows = conn.execute(
            """
            SELECT id, approved_planned_for, scheduled_at
            FROM publish_jobs
            WHERE id IN (?, ?)
            ORDER BY id
            """,
            (legacy_job_id, exact_job_id),
        ).fetchall()
        assert rows == [
            (legacy_job_id, None, None),
            (exact_job_id, now, "2026-08-11 00:05:00"),
        ]

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                UPDATE publish_jobs
                SET approved_planned_for = NULL
                WHERE id = ?
                """,
                (exact_job_id,),
            )
        conn.rollback()

    downgraded = _run_alembic(["downgrade", "a2f7c9e4d1b6"], tmp_db)
    assert downgraded.returncode == 0, f"planned time downgrade 失败: {downgraded.stderr}"
    with sqlite3.connect(str(tmp_db)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info('publish_jobs')")}
        assert "approved_planned_for" not in columns
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM publish_jobs WHERE id IN (?, ?)",
                (legacy_job_id, exact_job_id),
            ).fetchone()[0]
            == 2
        )


def test_asset_vault_metadata_and_content_snapshot_upgrade_downgrade(
    tmp_db: Path,
) -> None:
    """Agent revision is additive, fail-closed, and reversible over legacy assets."""

    before = _run_alembic(["upgrade", "f1c3b8a7d2e4"], tmp_db)
    assert before.returncode == 0, f"契约前迁移失败: {before.stderr}"
    with sqlite3.connect(str(tmp_db)) as conn:
        article_id, account_id, _ = _insert_legacy_job(conn)
        asset_id = _insert_legacy_asset(conn, article_id)

    upgraded = _run_alembic(["upgrade", "head"], tmp_db)
    assert upgraded.returncode == 0, f"契约迁移失败: {upgraded.stderr}"

    digest = "c" * 64
    now = "2026-08-11 00:00:00"
    snapshot = {
        "content_id": article_id,
        "title": "legacy article",
        "body": "body",
        "content_type": "long_article",
        "extra": {},
        "assets": [
            {
                "asset_id": asset_id,
                "asset_type": "image",
                "source": "user_upload",
                "storage_path": "/private/vault/content",
                "vaulted_path": f"vault://sha256/{digest}",
                "sha256": digest,
                "size_bytes": 7,
                "meta": {"role": "cover"},
            }
        ],
    }
    with sqlite3.connect(str(tmp_db)) as conn:
        # Legacy rows deliberately remain all-NULL until explicitly ingested.
        metadata = conn.execute(
            """
            SELECT content_sha256, size_bytes, storage_kind
            FROM assets WHERE id = ?
            """,
            (asset_id,),
        ).fetchone()
        assert metadata == (None, None, None)

        conn.execute(
            """
            UPDATE assets
            SET content_sha256 = ?, size_bytes = ?, storage_kind = ?
            WHERE id = ?
            """,
            (digest, 7, "agent_vault_v1", asset_id),
        )
        conn.commit()

        # Partial, unsupported, malformed, and negative metadata combinations
        # must all fail at the database boundary, not only in ORM validation.
        invalid_metadata = [
            (None, 7, "agent_vault_v1"),
            (digest, None, "agent_vault_v1"),
            (digest, 7, None),
            (digest, 7, "future_vault"),
            ("d" * 63, 7, "agent_vault_v1"),
            (digest, -1, "agent_vault_v1"),
        ]
        for content_sha256, size_bytes, storage_kind in invalid_metadata:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    UPDATE assets
                    SET content_sha256 = ?, size_bytes = ?, storage_kind = ?
                    WHERE id = ?
                    """,
                    (content_sha256, size_bytes, storage_kind, asset_id),
                )
            conn.rollback()

        plan_id = conn.execute(
            """
            INSERT INTO publication_plans
                (article_id, state, content_digest, plan_digest, content_snapshot,
                 targets, planned_for, created_by, created_by_type, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                article_id,
                "approval_pending",
                digest,
                digest,
                json.dumps(snapshot, separators=(",", ":")),
                json.dumps([{"account_id": account_id, "platform": "zhihu"}]),
                now,
                "agent-a",
                "agent",
                now,
                now,
            ),
        ).lastrowid
        assert plan_id is not None
        conn.commit()
        stored_snapshot = conn.execute(
            "SELECT content_snapshot FROM publication_plans WHERE id = ?",
            (plan_id,),
        ).fetchone()[0]
        assert json.loads(stored_snapshot) == snapshot
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "UPDATE publication_plans SET content_snapshot = NULL WHERE id = ?",
                (plan_id,),
            )
        conn.rollback()

    downgraded = _run_alembic(["downgrade", "f1c3b8a7d2e4"], tmp_db)
    assert downgraded.returncode == 0, f"Agent 契约降级失败: {downgraded.stderr}"
    with sqlite3.connect(str(tmp_db)) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "publication_plans",
            "approval_requests",
            "agent_operations",
        }.isdisjoint(tables)
        asset_columns = {row[1] for row in conn.execute("PRAGMA table_info('assets')")}
        assert {
            "content_sha256",
            "size_bytes",
            "storage_kind",
        }.isdisjoint(asset_columns)
        legacy_asset = conn.execute(
            """
            SELECT article_id, asset_type, source, local_path, meta
            FROM assets WHERE id = ?
            """,
            (asset_id,),
        ).fetchone()
        assert legacy_asset == (
            article_id,
            "image",
            "user_upload",
            "legacy/cover.png",
            '{"role":"cover"}',
        )


def test_agent_contract_state_constraints_fail_closed(tmp_db: Path) -> None:
    result = _run_alembic(["upgrade", "head"], tmp_db)
    assert result.returncode == 0, f"upgrade 失败: {result.stderr}"

    now = "2026-08-11 00:00:00"
    digest = "b" * 64
    with sqlite3.connect(str(tmp_db)) as conn:
        article_id, _, _ = _insert_legacy_job(conn)
        plan_ids: list[int] = []
        for state in (
            "draft",
            "approval_pending",
            "approved",
            "rejected",
            "scheduled",
            "cancelled",
            "expired",
        ):
            plan_id = conn.execute(
                """
                INSERT INTO publication_plans
                    (article_id, state, content_digest, plan_digest, content_snapshot,
                     targets, planned_for, created_by, created_by_type, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article_id,
                    state,
                    digest,
                    digest,
                    "{}",
                    "[]",
                    now,
                    "agent-a",
                    "agent",
                    now,
                    now,
                ),
            ).lastrowid
            assert plan_id is not None
            plan_ids.append(int(plan_id))
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO publication_plans
                    (article_id, state, content_digest, plan_digest, content_snapshot,
                     targets, planned_for, created_by, created_by_type, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article_id,
                    "executing",
                    digest,
                    digest,
                    "{}",
                    "[]",
                    now,
                    "agent-a",
                    "agent",
                    now,
                    now,
                ),
            )
        conn.rollback()

        # An approval/rejection is not a valid terminal decision without the
        # authenticated decider identity and decision timestamp.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO approval_requests
                    (plan_id, plan_digest, status, requested_by,
                     requested_by_type, requested_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (plan_ids[0], digest, "approved", "agent-a", "agent", now, now),
            )
        conn.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO agent_operations
                    (principal_id, principal_type, operation, idempotency_key,
                     request_digest, response_status_code, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("agent-a", "agent", "stage_content", "request-2", digest, 799, now, now),
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
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    business_tables = {
        "topics",
        "accounts",
        "articles",
        "assets",
        "publish_jobs",
        "metrics",
        "publication_plans",
        "approval_requests",
        "agent_operations",
    }
    leftover = business_tables & tables
    assert not leftover, (
        f"downgrade base 后业务表应清空，但残留: {leftover}（全部表: {sorted(tables)}）"
    )
