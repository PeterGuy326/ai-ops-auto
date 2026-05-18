"""tests/test_metrics_source.py — Round 6 · Metrics.source 字段 + 触发判定升级回归套件。

战场：
  - src/ai_ops/core/models.py:Metrics.source 字段（新增）
  - src/ai_ops/scheduler/metrics.py:collect_one source 参数 + 三段优先级触发判定
  - src/ai_ops/scheduler/worker.py:_persist_initial_metrics source="initial"
  - src/ai_ops/api/main.py:/jobs/{id}/collect source="manual"
  - alembic/versions/7c7c50aecd6a_add_metrics_source.py (ALTER ADD COLUMN with server_default)

背景（owner 价值）：
  上轮触发判定靠"计数 + 时间窗反推"（二阶推导），未来加任何 Metrics 写入入口
  （backfill / 第三方 API 同步）都可能污染。Round 6 加 source 字段后：
    - 每条 Metrics 自带 source 一阶语义
    - 触发判定升级到三段优先级：
        priority 1: interval_index 显式飞轮路径（最稳）
        priority 2: source-based scheduled count（owner 终态判定）
        priority 3: cutoff + count 兜底（守护测试 / 生产 ALTER 瞬间）

测试契约（7 用例）：
  1. worker._persist_initial_metrics 写的 Metrics.source == "initial"
  2. collect_one 默认 source="scheduled" 落 Metrics.source == "scheduled"
  3. API /jobs/{id}/collect 端点写的 Metrics.source == "manual"
  4. source-based 触发：1 initial + 1 manual + 1 scheduled → 不触发；再写第 2 个 scheduled → 触发
  5. interval_index 优先级覆盖 source count：显式 idx=HEALTH_EVAL_INTERVAL_INDEX 直接触发
  6. alembic upgrade head 后老 Metrics 行 source 默认 "scheduled"（server_default 生效）
  7. [bonus] 未知 source 值（"weird"）不影响 scheduled count 判定

session 模板：沿用 P9 收口的"单一信任源" SessionLocal.configure(bind=engine)。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from ai_ops.core import db as db_mod
from ai_ops.core.enums import (
    AccountHealth,
    ArticleStatus,
    ContentType,
    JobStatus,
    Platform,
)
from ai_ops.core.models import (
    Account,
    Article,
    Base,
    Metrics,
    PublishJob,
    Topic,
)
from ai_ops.core.schemas import PublishResult


_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixture：rebind 生产 SessionLocal 到 in-memory engine（与上几轮同套路）
# ---------------------------------------------------------------------------


@pytest.fixture
def production_session_in_memory():
    """rebind 生产 SessionLocal 到 in-memory engine。

    P9 上轮收口的单一信任源约定：保留 production kwargs（expire_on_commit=False）。
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    original_bind = db_mod.SessionLocal.kw.get("bind")
    db_mod.SessionLocal.configure(bind=engine)
    try:
        yield db_mod.SessionLocal
    finally:
        db_mod.SessionLocal.configure(bind=original_bind)
        engine.dispose()


# ---------------------------------------------------------------------------
# 通用 chain 构造
# ---------------------------------------------------------------------------


def _mk_publishable_chain(SessionLocal, *, finished_at: datetime | None = None) -> int:
    """构造最小可发布链路 (topic → account → article → job)，返回 job_id。"""
    with SessionLocal() as s:
        topic = Topic(
            name=f"t_source_{id(s)}",
            keywords=[],
            persona={},
            target_platforms=[],
        )
        s.add(topic)
        s.flush()
        acc = Account(
            platform=Platform.TOUTIAO,
            nickname="acc_source",
            health=AccountHealth.HEALTHY,
            encrypted_credential=b"",
        )
        s.add(acc)
        s.flush()
        article = Article(
            topic_id=topic.id,
            title="source test title",
            body="正文，干净的",
            content_type=ContentType.IMAGE_TEXT,
            status=ArticleStatus.PUBLISHING if finished_at is None else ArticleStatus.PUBLISHED,
            extra={},
        )
        s.add(article)
        s.flush()
        job = PublishJob(
            article_id=article.id,
            account_id=acc.id,
            platform=Platform.TOUTIAO,
            status=JobStatus.PENDING if finished_at is None else JobStatus.SUCCESS,
            publisher_kind="toutiao",
            attempts=0 if finished_at is None else 1,
            max_attempts=3,
            platform_post_id=None if finished_at is None else "post_src_test",
            platform_url=None if finished_at is None else "http://example.com/src_test",
            started_at=None if finished_at is None else (finished_at - timedelta(minutes=2)),
            finished_at=finished_at,
        )
        s.add(job)
        s.commit()
        return job.id


def _patch_worker_externals(monkeypatch):
    """统一桩掉 worker 外部依赖。"""
    from ai_ops.accounts.manager import RateCheckResult
    from ai_ops.scheduler import worker as worker_mod

    monkeypatch.setattr(worker_mod, "get_credential", lambda s, aid: {"fake": "cred"})
    monkeypatch.setattr(
        worker_mod,
        "check_rate_limit",
        lambda s, aid: RateCheckResult(allowed=True, reason=""),
    )
    monkeypatch.setattr(worker_mod, "mark_published", lambda s, aid: None)
    monkeypatch.setattr(worker_mod, "is_paused", lambda acc: False)

    from ai_ops.scheduler import metrics as metrics_mod
    monkeypatch.setattr(metrics_mod, "schedule_after_publish", lambda jid: [])

    import ai_ops.notify as notify_mod
    monkeypatch.setattr(notify_mod, "publish_success", lambda snap: None)
    monkeypatch.setattr(notify_mod, "publish_failed", lambda snap: None)


def _patch_metrics_externals(monkeypatch, *, collected_views: int = 200):
    """统一桩掉 collect_one 外部依赖。"""
    from ai_ops.scheduler import metrics as metrics_mod

    monkeypatch.setattr(metrics_mod, "get_credential", lambda s, aid: {"fake": "cred"})

    async def fake_collect(post_id, post_url, cred):
        return {
            "likes": int(collected_views * 0.05),
            "comments": int(collected_views * 0.01),
            "shares": 0,
            "views": collected_views,
            "raw": {},
        }

    fake_pub = MagicMock()
    fake_pub.collect_metrics = fake_collect
    monkeypatch.setattr(metrics_mod.default_registry, "resolve", lambda platform: [fake_pub])

    import ai_ops.content.heat_engine as he_mod
    monkeypatch.setattr(he_mod, "recompute_topic_heat_for_article", lambda aid: None)


def _install_eval_spy(monkeypatch):
    """spy evaluate_after_metrics：返回 healthy 动作 + 记录 call_count。"""
    import ai_ops.accounts.health_monitor as hm_mod

    calls: list[int] = []

    def spy(s, jid):
        calls.append(jid)
        return SimpleNamespace(decision="healthy", reason="spy")

    monkeypatch.setattr(hm_mod, "evaluate_after_metrics", spy)
    return calls


def _run_execute_job_with_result(monkeypatch, job_id: int, result: PublishResult):
    """跑 execute_job 让 _try_publishers 返回指定 result。"""
    from ai_ops.scheduler import worker as worker_mod

    async def fake_try_publishers(platform, account_id, credential, content):
        return result
    monkeypatch.setattr(worker_mod, "_try_publishers", fake_try_publishers)
    return asyncio.run(worker_mod.execute_job(job_id))


# ---------------------------------------------------------------------------
# Case 1: worker._persist_initial_metrics 写的 source == "initial"
# ---------------------------------------------------------------------------


class TestInitialSource:
    """worker._persist_initial_metrics 路径必须落 source='initial'。"""

    def test_persist_initial_metrics_sets_source_initial(
        self, production_session_in_memory, monkeypatch
    ):
        """完整 initial_metadata → 落库的 Metrics.source == 'initial'。

        Round 6 核心契约：worker 路径写的快照能被 source-based 触发判定排除。
        """
        SessionLocal = production_session_in_memory
        job_id = _mk_publishable_chain(SessionLocal)
        _patch_worker_externals(monkeypatch)

        result = PublishResult(
            success=True,
            platform_post_id="p_init_src",
            platform_url="http://example.com/init_src",
            raw_response={
                "initial_metadata": {
                    "url": "http://example.com/init_src",
                    "view_count": "1.2万",
                    "like_count": "300",
                    "comment_count": "20",
                    "share_count": "5",
                },
            },
        )

        run_result = _run_execute_job_with_result(monkeypatch, job_id, result)
        assert run_result.success is True

        with SessionLocal() as s:
            metrics = s.query(Metrics).filter(Metrics.job_id == job_id).all()
            assert len(metrics) == 1, "应当落 1 行 initial Metrics"
            assert metrics[0].source == "initial", (
                f"worker._persist_initial_metrics 路径 source 必须 = 'initial'，"
                f"实际 '{metrics[0].source}'"
            )


# ---------------------------------------------------------------------------
# Case 2: collect_one 默认 source == "scheduled"
# ---------------------------------------------------------------------------


class TestScheduledSource:
    """collect_one 默认路径必须落 source='scheduled'。"""

    def test_collect_one_default_source_scheduled(
        self, production_session_in_memory, monkeypatch
    ):
        """不传 source → 落库的 Metrics.source == 'scheduled'（飞轮默认路径）。"""
        from ai_ops.scheduler import metrics as metrics_mod

        SessionLocal = production_session_in_memory
        now = datetime.utcnow()
        finished_at = now - timedelta(hours=1)
        job_id = _mk_publishable_chain(SessionLocal, finished_at=finished_at)

        _patch_metrics_externals(monkeypatch)
        _install_eval_spy(monkeypatch)  # 不关心是否触发；只验 source 字段

        asyncio.run(metrics_mod.collect_one(job_id))  # 不传 source

        with SessionLocal() as s:
            metrics = s.query(Metrics).filter(Metrics.job_id == job_id).all()
            assert len(metrics) == 1
            assert metrics[0].source == "scheduled", (
                f"collect_one 默认 source 必须 = 'scheduled'，实际 '{metrics[0].source}'"
            )


# ---------------------------------------------------------------------------
# Case 3: API /jobs/{id}/collect 端点写的 source == "manual"
# ---------------------------------------------------------------------------


class TestManualSource:
    """API /jobs/{id}/collect 端点必须传 source='manual' 给 collect_one。

    设计选择：不走 TestClient 真起 app（TestClient 触发 lifespan 重置 SessionLocal
    + sqlite 跨线程限制，会污染 in-memory engine fixture），而是直接调端点函数 +
    spy collect_one 抓 source 参数 + 真跑链路验证落库。这等价于 TestClient 路径的
    业务契约，但避开了 starlette / sqlite 跨线程冲突。
    """

    def test_api_collect_endpoint_writes_source_manual(
        self, production_session_in_memory, monkeypatch
    ):
        """直接调 api_collect_metrics → 它调 collect_one(job_id, source='manual')。

        Round 6：运营手动复采的 metric 必须被显式标 source='manual'，
        避免手动操作污染 24h 健康度评估节奏（priority 2 source-based 计数排除）。
        """
        from ai_ops.api import main as api_mod
        from ai_ops.scheduler import metrics as metrics_mod

        SessionLocal = production_session_in_memory
        now = datetime.utcnow()
        finished_at = now - timedelta(hours=1)
        job_id = _mk_publishable_chain(SessionLocal, finished_at=finished_at)

        _patch_metrics_externals(monkeypatch)
        _install_eval_spy(monkeypatch)

        # spy collect_one：抓 source 参数，再真跑原函数（落 Metrics 行）
        captured_kwargs: list[dict] = []
        original_collect_one = metrics_mod.collect_one

        async def spy_collect_one(jid, **kwargs):
            captured_kwargs.append(dict(kwargs))
            return await original_collect_one(jid, **kwargs)

        # 必须 patch metrics_mod 的 collect_one——api 端点 import 时拿的是该属性
        monkeypatch.setattr(metrics_mod, "collect_one", spy_collect_one)

        # 直接调端点函数（绕过 FastAPI / Starlette 路由层 + lifespan）
        result = asyncio.run(api_mod.api_collect_metrics(job_id))
        assert result is not None and not result.get("skipped"), (
            f"端点应正常采集，实际 result={result}"
        )

        # 契约 1：端点必须以 source='manual' 调 collect_one
        assert captured_kwargs == [{"source": "manual"}], (
            f"端点必须传 source='manual'，实际 kwargs={captured_kwargs}"
        )

        # 契约 2：真正落库的 Metrics 行 source='manual'
        with SessionLocal() as s:
            metrics = s.query(Metrics).filter(Metrics.job_id == job_id).all()
            assert len(metrics) == 1
            assert metrics[0].source == "manual", (
                f"API /jobs/{{id}}/collect 路径 source 必须 = 'manual'，"
                f"实际 '{metrics[0].source}'"
            )


# ---------------------------------------------------------------------------
# Case 4: source-based 触发计数（owner 终态判定）
# ---------------------------------------------------------------------------


class TestSourceBasedTrigger:
    """触发判定优先级 2：source-based scheduled count。

    依赖前提：表中至少 1 条非 'scheduled' 的 metric（区分已生效）。
    """

    def test_eval_trigger_counts_only_scheduled_source(
        self, production_session_in_memory, monkeypatch
    ):
        """构造 1 initial + 1 manual + 1 scheduled → 不触发；
        再写第 2 个 scheduled → 触发（owner 终态契约）。

        飞轮档位 3 档（HEALTH_EVAL_INTERVAL_INDEX=1 → 24h 节点）：
          - 第 1 次飞轮（已写 1 scheduled，含本次）→ count=1，不触发
          - 第 2 次飞轮（再写 1 scheduled，合计 2）→ count=2 == HEALTH_EVAL_INTERVAL_INDEX+1 → 触发
        """
        from ai_ops.scheduler import metrics as metrics_mod

        SessionLocal = production_session_in_memory
        now = datetime.utcnow()
        finished_at = now - timedelta(hours=25)
        job_id = _mk_publishable_chain(SessionLocal, finished_at=finished_at)

        # 预塞：1 initial + 1 manual（让"source 区分已生效"路径激活）
        with SessionLocal() as s:
            s.add(Metrics(
                job_id=job_id,
                views=100, likes=5, comments=1, shares=0,
                source="initial",
                collected_at=finished_at,
                raw={},
            ))
            s.add(Metrics(
                job_id=job_id,
                views=150, likes=8, comments=2, shares=1,
                source="manual",
                collected_at=finished_at + timedelta(minutes=10),
                raw={},
            ))
            s.commit()

        _patch_metrics_externals(monkeypatch, collected_views=500)
        eval_calls = _install_eval_spy(monkeypatch)

        # 第 1 次飞轮：写 1 条 scheduled → count(scheduled)=1，不触发
        asyncio.run(metrics_mod.collect_one(job_id))
        assert eval_calls == [], (
            f"1 initial + 1 manual + 1 scheduled 时不应触发，"
            f"实际 calls={eval_calls}"
        )

        # 第 2 次飞轮：写第 2 条 scheduled → count(scheduled)=2 == HEALTH_EVAL_INTERVAL_INDEX+1 → 触发
        asyncio.run(metrics_mod.collect_one(job_id))
        assert eval_calls == [job_id], (
            f"第 2 个 scheduled 落库后应触发 1 次，实际 calls={eval_calls}"
        )

        # 副验证：表中确实有 4 行 metric（1 initial + 1 manual + 2 scheduled）
        with SessionLocal() as s:
            total = s.query(Metrics).filter(Metrics.job_id == job_id).count()
            scheduled_total = (
                s.query(Metrics)
                .filter(Metrics.job_id == job_id, Metrics.source == "scheduled")
                .count()
            )
            assert total == 4, f"总行数应 4，实际 {total}"
            assert scheduled_total == 2, f"scheduled 行数应 2，实际 {scheduled_total}"


# ---------------------------------------------------------------------------
# Case 5: interval_index 优先级覆盖 source count
# ---------------------------------------------------------------------------


class TestIntervalIndexOverridesSource:
    """优先级 1（interval_index）必须无视 source count 直接判定。"""

    def test_eval_trigger_with_interval_index_overrides_count(
        self, production_session_in_memory, monkeypatch
    ):
        """显式 interval_index=HEALTH_EVAL_INTERVAL_INDEX → 触发，
        即使 source count 还没到（priority 1 必须无视 priority 2）。
        """
        from ai_ops.scheduler import metrics as metrics_mod

        SessionLocal = production_session_in_memory
        now = datetime.utcnow()
        finished_at = now - timedelta(hours=24)
        job_id = _mk_publishable_chain(SessionLocal, finished_at=finished_at)

        # 预塞 1 initial + 1 manual，但 0 scheduled —— source-based 计数下不应触发
        with SessionLocal() as s:
            s.add(Metrics(
                job_id=job_id, views=10, likes=0, comments=0, shares=0,
                source="initial", collected_at=finished_at, raw={},
            ))
            s.add(Metrics(
                job_id=job_id, views=20, likes=0, comments=0, shares=0,
                source="manual", collected_at=finished_at + timedelta(minutes=5), raw={},
            ))
            s.commit()

        _patch_metrics_externals(monkeypatch, collected_views=1000)
        eval_calls = _install_eval_spy(monkeypatch)

        # 显式 interval_index=HEALTH_EVAL_INTERVAL_INDEX → priority 1 直接触发
        asyncio.run(metrics_mod.collect_one(
            job_id,
            interval_index=metrics_mod.HEALTH_EVAL_INTERVAL_INDEX,
        ))

        assert eval_calls == [job_id], (
            f"interval_index=HEALTH_EVAL_INTERVAL_INDEX 必须无视 source count 直接触发，"
            f"实际 calls={eval_calls}"
        )


# ---------------------------------------------------------------------------
# Case 6: alembic migration 真演进 → server_default 落实
# ---------------------------------------------------------------------------


class TestAlembicMigrationServerDefault:
    """alembic upgrade head 后 metrics.source 字段存在 + server_default='scheduled' 生效。

    这是"第一次真用 alembic workflow 演进字段"的硬证据 —— 全新部署链路验证。
    """

    @pytest.fixture(autouse=True)
    def _ensure_alembic_available(self):
        if shutil.which("alembic") is None:
            pytest.skip("alembic CLI 未安装；本测试需要 alembic 可执行文件在 PATH")

    def test_migration_adds_source_with_server_default(self, tmp_path):
        """空 DB → alembic upgrade head → metrics.source 字段存在且 NOT NULL 且
        默认 'scheduled'；ALTER 后预塞老行不传 source 也能落库（server_default 兜底）。
        """
        db = tmp_path / "round6_src.db"
        env = os.environ.copy()
        env["DATABASE_URL"] = f"sqlite:///{db.resolve()}"

        result = subprocess.run(
            ["alembic", "upgrade", "head"],
            cwd=str(_REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, (
            f"alembic upgrade head 失败:\nstdout={result.stdout}\nstderr={result.stderr}"
        )

        # 字段存在 + NOT NULL + 默认 'scheduled'
        with sqlite3.connect(str(db)) as conn:
            rows = list(conn.execute(
                "SELECT name, type, \"notnull\", dflt_value FROM pragma_table_info('metrics')"
            ))
            cols = {r[0]: r for r in rows}
        assert "source" in cols, f"upgrade head 后 metrics 应有 source 列，实际列: {list(cols)}"
        _, col_type, notnull, dflt = cols["source"]
        assert "VARCHAR" in col_type.upper() or "STRING" in col_type.upper() or "TEXT" in col_type.upper(), (
            f"source 列类型应为 VARCHAR，实际 {col_type}"
        )
        assert notnull == 1, f"source 列应 NOT NULL，pragma 实际 notnull={notnull}"
        assert dflt is not None and "scheduled" in dflt, (
            f"source 列 server_default 应含 'scheduled'，实际 {dflt}"
        )

        # 真插一行不传 source 的 metric（先建依赖链）—— server_default 必须兜底为 'scheduled'
        with sqlite3.connect(str(db)) as conn:
            conn.execute(
                "INSERT INTO topics (name, category, keywords, persona, target_platforms, "
                "heat_score, notes, created_at) VALUES "
                "('t_mig', 'general', '[]', '{}', '[]', 0.0, '', '2026-01-01 00:00:00')"
            )
            topic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO accounts (platform, nickname, profile, encrypted_credential, "
                "health, risk_level, daily_quota, created_at) VALUES "
                "('toutiao', 'a_mig', '{}', X'', 'healthy', 0, 5, '2026-01-01 00:00:00')"
            )
            acc_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO articles (topic_id, title, body, content_type, status, "
                "target_platforms, target_account_ids, extra, created_at, updated_at) VALUES "
                "(?, 't', 'b', 'image_text', 'published', '[]', '[]', '{}', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')",
                (topic_id,),
            )
            art_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO publish_jobs (article_id, account_id, platform, status, "
                "publisher_kind, attempts, max_attempts, raw_response, created_at) VALUES "
                "(?, ?, 'toutiao', 'success', 'toutiao', 1, 3, '{}', '2026-01-01 00:00:00')",
                (art_id, acc_id),
            )
            job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # 关键：不传 source 列
            conn.execute(
                "INSERT INTO metrics (job_id, collected_at, likes, comments, shares, views, raw) "
                "VALUES (?, '2026-01-01 00:00:00', 0, 0, 0, 0, '{}')",
                (job_id,),
            )
            conn.commit()

            # server_default 应已兜底填 'scheduled'
            src_val = conn.execute(
                "SELECT source FROM metrics WHERE job_id = ?", (job_id,)
            ).fetchone()[0]
        assert src_val == "scheduled", (
            f"INSERT 不传 source 时 server_default 应兜底 'scheduled'，实际 '{src_val}'"
        )


# ---------------------------------------------------------------------------
# Case 7 [bonus]: 未知 source 值不破坏 scheduled count 判定
# ---------------------------------------------------------------------------


class TestUnknownSourceDoesNotBreakCount:
    """防御性测试：手动塞个 source='weird' 不会污染 scheduled count。"""

    def test_source_unknown_value_does_not_break_count(
        self, production_session_in_memory, monkeypatch
    ):
        """构造 1 'weird' source + 1 scheduled → 不触发（'weird' 算非 scheduled，
        激活 priority 2；scheduled count=1，未达 HEALTH_EVAL_INTERVAL_INDEX+1）。
        """
        from ai_ops.scheduler import metrics as metrics_mod

        SessionLocal = production_session_in_memory
        now = datetime.utcnow()
        finished_at = now - timedelta(hours=25)
        job_id = _mk_publishable_chain(SessionLocal, finished_at=finished_at)

        with SessionLocal() as s:
            s.add(Metrics(
                job_id=job_id, views=10, likes=0, comments=0, shares=0,
                source="weird", collected_at=finished_at + timedelta(minutes=10), raw={},
            ))
            s.commit()

        _patch_metrics_externals(monkeypatch, collected_views=500)
        eval_calls = _install_eval_spy(monkeypatch)

        # 第 1 次飞轮 → scheduled count = 1（含本次），不触发
        asyncio.run(metrics_mod.collect_one(job_id))
        assert eval_calls == [], (
            f"1 weird + 1 scheduled 应不触发（scheduled count=1）, 实际 {eval_calls}"
        )

        # 第 2 次飞轮 → scheduled count = 2，触发
        asyncio.run(metrics_mod.collect_one(job_id))
        assert eval_calls == [job_id], (
            f"再写 1 scheduled 后 count=2，应触发，实际 {eval_calls}"
        )
