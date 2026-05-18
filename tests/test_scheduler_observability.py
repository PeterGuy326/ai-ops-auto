"""tests/test_scheduler_observability.py — Task B · scheduler 残余黑洞回归单测。

P7-X 上轮把 worker.py 3 处黑洞补上 capture_exception，本套件覆盖 scheduler
模块剩余 6 处 + worker.py simhash 兜底 1 处，共 7 处一把梭闭环。

覆盖：
  1. health.py 3 处
     - credential_load: 凭证拿取失败被 capture(scope="scheduler.health.credential_load")
     - check:           publisher.health_check 抛被 capture(scope="scheduler.health.check")
     - notify:          notify.account_expired 抛被 capture(scope="scheduler.health.notify")
  2. metrics.py 2 处
     - health_eval:     evaluate_after_metrics 抛被 capture(scope="scheduler.metrics.health_eval")
     - heat_refresh:    recompute_topic_heat_for_article 抛被 capture(scope="scheduler.metrics.heat_refresh")
  3. queue.py 1 处
     - cancel:          APScheduler.remove_job 抛被 capture(scope="scheduler.queue.cancel")
  4. worker.py 1 处（_pre_publish_check 的 simhash 兜底）
     - simhash_check:   similarity_checker 抛被 capture(scope="worker.simhash_check")，
                        且返回 (True, None) 不阻断主路径

策略：
  - mock 各模块顶部 from-import 的 capture_exception 名字，验调用 + scope + ctx
  - 不跑真实 APScheduler / publisher / DB 全链路，每个用例最小化构造
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine

from ai_ops.core.db import SessionLocal as _ProdSessionLocal
from ai_ops.core.enums import (
    AccountHealth,
    ArticleStatus,
    ContentType,
    JobStatus,
    Platform,
)
from ai_ops.core.models import Account, Article, Base, PublishJob, Topic


# ---------------------------------------------------------------------------
# 通用：in-memory engine + capture 收集器
# ---------------------------------------------------------------------------


def _build_engine_and_session():
    """复用 production SessionLocal 配置，只重绑到 in-memory engine。

    避免和 core/db.py 的 production sessionmaker 配置漂移（TD-X4 修复后约定：
    测试和生产共用同一 sessionmaker 配置，不另起临时 sessionmaker）。
    """
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    original_bind = _ProdSessionLocal.kw.get("bind")
    _ProdSessionLocal.configure(bind=engine)
    _ProdSessionLocal._test_original_bind = original_bind  # type: ignore[attr-defined]
    return _ProdSessionLocal


def _install_session_scope(monkeypatch, module, SessionLocal):
    """把目标 scheduler 模块的 session_scope 重定向到 in-memory SessionLocal。"""

    @contextmanager
    def fake_scope():
        s = SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(module, "session_scope", fake_scope)


def _install_capture(monkeypatch, module):
    """把目标模块顶部 from-import 的 capture_exception 替换成收集器。

    返回 list，每次调用 append 一条 {"exc_type": ..., "exc_msg": ..., **ctx}。
    """
    capture_calls: list[dict] = []

    def fake_capture(exc, **ctx):
        capture_calls.append({"exc_type": type(exc).__name__, "exc_msg": str(exc), **ctx})
        return True

    monkeypatch.setattr(module, "capture_exception", fake_capture)
    return capture_calls


# ---------------------------------------------------------------------------
# Part 1: health.py 3 处黑洞
# ---------------------------------------------------------------------------


def _mk_account(SessionLocal, *, health=AccountHealth.HEALTHY, platform=Platform.XIAOHONGSHU):
    with SessionLocal() as s:
        acc = Account(
            platform=platform,
            nickname="acc_health",
            health=health,
            encrypted_credential=b"",
        )
        s.add(acc)
        s.commit()
        return acc.id


class TestHealthObservabilityHooks:
    """health.py 3 处 except 接 capture 验证。"""

    def test_health_credential_load_failure_captured(self, monkeypatch):
        """get_credential 抛 → capture(scope='scheduler.health.credential_load') + account_id。"""
        from ai_ops.scheduler import health as health_mod

        SessionLocal = _build_engine_and_session()
        account_id = _mk_account(SessionLocal)
        _install_session_scope(monkeypatch, health_mod, SessionLocal)
        capture_calls = _install_capture(monkeypatch, health_mod)

        def boom_get_credential(s, aid):
            raise RuntimeError("vault offline")

        monkeypatch.setattr(health_mod, "get_credential", boom_get_credential)

        # 让后续 publisher 路径也不抛——只关注 credential_load 黑洞
        # publisher.resolve 返回 [] 让 check 走 continue，避免别的 capture 污染
        monkeypatch.setattr(
            health_mod.default_registry, "resolve", lambda platform: []
        )

        asyncio.run(health_mod.check_all_accounts())

        creds = [c for c in capture_calls if c.get("scope") == "scheduler.health.credential_load"]
        assert len(creds) == 1, f"expected 1 credential capture, got {capture_calls}"
        assert creds[0]["exc_type"] == "RuntimeError"
        assert "vault offline" in creds[0]["exc_msg"]
        assert creds[0]["account_id"] == account_id

    def test_health_check_failure_captured(self, monkeypatch):
        """publisher.health_check 抛 → capture(scope='scheduler.health.check') + account_id/platform。"""
        from ai_ops.scheduler import health as health_mod

        SessionLocal = _build_engine_and_session()
        account_id = _mk_account(SessionLocal)
        _install_session_scope(monkeypatch, health_mod, SessionLocal)
        capture_calls = _install_capture(monkeypatch, health_mod)

        monkeypatch.setattr(health_mod, "get_credential", lambda s, aid: {})

        # 构造一个 publisher 让 health_check 抛
        async def boom_health_check(aid, cred):
            raise RuntimeError("api 500")

        fake_pub = MagicMock()
        fake_pub.health_check = boom_health_check
        monkeypatch.setattr(
            health_mod.default_registry, "resolve", lambda platform: [fake_pub]
        )
        # update_health 不要真改 DB（避免 fake account 字段不全炸）
        monkeypatch.setattr(health_mod, "update_health", lambda s, aid, h: None)

        asyncio.run(health_mod.check_all_accounts())

        checks = [c for c in capture_calls if c.get("scope") == "scheduler.health.check"]
        assert len(checks) == 1, f"expected 1 check capture, got {capture_calls}"
        cap = checks[0]
        assert cap["exc_type"] == "RuntimeError"
        assert "api 500" in cap["exc_msg"]
        assert cap["account_id"] == account_id
        assert "platform" in cap

    def test_health_notify_failure_captured(self, monkeypatch):
        """account_expired 抛 → capture(scope='scheduler.health.notify') + account_id。

        前置：health_check 返回 EXPIRED 才会进 notify 分支。
        """
        from ai_ops.scheduler import health as health_mod

        SessionLocal = _build_engine_and_session()
        account_id = _mk_account(SessionLocal)
        _install_session_scope(monkeypatch, health_mod, SessionLocal)
        capture_calls = _install_capture(monkeypatch, health_mod)

        monkeypatch.setattr(health_mod, "get_credential", lambda s, aid: {})

        async def expired_health_check(aid, cred):
            return AccountHealth.EXPIRED

        fake_pub = MagicMock()
        fake_pub.health_check = expired_health_check
        monkeypatch.setattr(
            health_mod.default_registry, "resolve", lambda platform: [fake_pub]
        )
        monkeypatch.setattr(health_mod, "update_health", lambda s, aid, h: None)

        # 让 from ..notify import account_expired 拿到的 account_expired 抛
        import ai_ops.notify as notify_mod

        def boom_notify(snapshot):
            raise RuntimeError("feishu webhook down")

        monkeypatch.setattr(notify_mod, "account_expired", boom_notify)

        asyncio.run(health_mod.check_all_accounts())

        notifies = [c for c in capture_calls if c.get("scope") == "scheduler.health.notify"]
        assert len(notifies) == 1, f"expected 1 notify capture, got {capture_calls}"
        cap = notifies[0]
        assert cap["exc_type"] == "RuntimeError"
        assert "feishu webhook down" in cap["exc_msg"]
        assert cap["account_id"] == account_id


# ---------------------------------------------------------------------------
# Part 2: metrics.py 2 处黑洞
# ---------------------------------------------------------------------------


def _mk_metrics_job(SessionLocal):
    """构造一个 collect_one 能跑到 metric_count==2 节点的 job。

    需要先塞 1 条已存在的 Metrics（让本次 collect 后 count=2），
    job 必须有 platform_post_id。
    """
    from ai_ops.core.models import Metrics

    with SessionLocal() as s:
        topic = Topic(name="t", keywords=[], persona={}, target_platforms=[])
        s.add(topic)
        s.flush()
        acc = Account(
            platform=Platform.XIAOHONGSHU,
            nickname="acc_m",
            health=AccountHealth.HEALTHY,
            encrypted_credential=b"",
        )
        s.add(acc)
        s.flush()
        article = Article(
            topic_id=topic.id,
            title="t",
            body="x",
            content_type=ContentType.IMAGE_TEXT,
            status=ArticleStatus.PUBLISHED,
            extra={},
        )
        s.add(article)
        s.flush()
        job = PublishJob(
            article_id=article.id,
            account_id=acc.id,
            platform=Platform.XIAOHONGSHU,
            status=JobStatus.SUCCESS,
            publisher_kind="test",
            attempts=1,
            max_attempts=3,
            platform_post_id="post_123",
            platform_url="http://x/post_123",
        )
        s.add(job)
        s.flush()
        # 已经有 1 条 metric，本次 collect 后 count=2 触发 health_eval
        s.add(Metrics(job_id=job.id, likes=0, comments=0, shares=0, views=0, raw={}))
        s.commit()
        return job.id, article.id


class TestMetricsObservabilityHooks:

    def test_metrics_health_eval_failure_captured(self, monkeypatch):
        """24h 节点 evaluate_after_metrics 抛 → capture(scope='scheduler.metrics.health_eval')。"""
        from ai_ops.scheduler import metrics as metrics_mod

        SessionLocal = _build_engine_and_session()
        job_id, article_id = _mk_metrics_job(SessionLocal)
        _install_session_scope(monkeypatch, metrics_mod, SessionLocal)
        capture_calls = _install_capture(monkeypatch, metrics_mod)

        monkeypatch.setattr(metrics_mod, "get_credential", lambda s, aid: {})

        # publisher.collect_metrics 返回 fake 数据
        async def fake_collect(post_id, post_url, cred):
            return {"likes": 1, "comments": 0, "shares": 0, "views": 10, "raw": {}}

        fake_pub = MagicMock()
        fake_pub.collect_metrics = fake_collect
        monkeypatch.setattr(
            metrics_mod.default_registry, "resolve", lambda platform: [fake_pub]
        )

        # 让 evaluate_after_metrics 抛 —— 它在 health_monitor 模块，from-import 在
        # except 内部局部 import，所以 mock 源头位置
        import ai_ops.accounts.health_monitor as hm_mod

        def boom_eval(s, jid):
            raise RuntimeError("health monitor exploded")

        monkeypatch.setattr(hm_mod, "evaluate_after_metrics", boom_eval)
        # heat_refresh 路径让它通过——避免污染断言
        import ai_ops.content.heat_engine as he_mod

        monkeypatch.setattr(he_mod, "recompute_topic_heat_for_article", lambda aid: None)

        asyncio.run(metrics_mod.collect_one(job_id))

        evals = [c for c in capture_calls if c.get("scope") == "scheduler.metrics.health_eval"]
        assert len(evals) == 1, f"expected 1 health_eval capture, got {capture_calls}"
        cap = evals[0]
        assert cap["exc_type"] == "RuntimeError"
        assert "health monitor exploded" in cap["exc_msg"]
        assert cap["job_id"] == job_id

    def test_metrics_heat_refresh_failure_captured(self, monkeypatch):
        """recompute_topic_heat_for_article 抛 → capture(scope='scheduler.metrics.heat_refresh')。"""
        from ai_ops.scheduler import metrics as metrics_mod

        SessionLocal = _build_engine_and_session()
        job_id, article_id = _mk_metrics_job(SessionLocal)
        _install_session_scope(monkeypatch, metrics_mod, SessionLocal)
        capture_calls = _install_capture(monkeypatch, metrics_mod)

        monkeypatch.setattr(metrics_mod, "get_credential", lambda s, aid: {})

        async def fake_collect(post_id, post_url, cred):
            return {"likes": 1, "comments": 0, "shares": 0, "views": 10, "raw": {}}

        fake_pub = MagicMock()
        fake_pub.collect_metrics = fake_collect
        monkeypatch.setattr(
            metrics_mod.default_registry, "resolve", lambda platform: [fake_pub]
        )

        # health_eval 路径通过，免污染
        import ai_ops.accounts.health_monitor as hm_mod
        from types import SimpleNamespace

        monkeypatch.setattr(
            hm_mod,
            "evaluate_after_metrics",
            lambda s, jid: SimpleNamespace(decision="keep", reason="ok"),
        )

        import ai_ops.content.heat_engine as he_mod

        def boom_heat(aid):
            raise RuntimeError("heat engine offline")

        monkeypatch.setattr(he_mod, "recompute_topic_heat_for_article", boom_heat)

        asyncio.run(metrics_mod.collect_one(job_id))

        heats = [c for c in capture_calls if c.get("scope") == "scheduler.metrics.heat_refresh"]
        assert len(heats) == 1, f"expected 1 heat capture, got {capture_calls}"
        cap = heats[0]
        assert cap["exc_type"] == "RuntimeError"
        assert "heat engine offline" in cap["exc_msg"]
        assert cap["job_id"] == job_id
        assert cap["article_id"] == article_id


# ---------------------------------------------------------------------------
# Part 3: queue.py 1 处黑洞（cancel）
# ---------------------------------------------------------------------------


class TestQueueObservabilityHooks:

    def test_queue_cancel_failure_captured(self, monkeypatch):
        """APScheduler.remove_job 抛 → capture(scope='scheduler.queue.cancel') + job_id。"""
        from ai_ops.scheduler import queue as queue_mod

        capture_calls = _install_capture(monkeypatch, queue_mod)

        # 直接造一个 TaskQueue 实例（不启 scheduler，避免线程残留）
        tq = queue_mod.TaskQueue()

        # 让 remove_job 抛
        def boom_remove(job_id):
            raise RuntimeError("job not found")

        monkeypatch.setattr(tq._scheduler, "remove_job", boom_remove)

        # 不抛 + 不返回值
        tq.cancel("ghost-job-42")

        cancels = [c for c in capture_calls if c.get("scope") == "scheduler.queue.cancel"]
        assert len(cancels) == 1, f"expected 1 cancel capture, got {capture_calls}"
        cap = cancels[0]
        assert cap["exc_type"] == "RuntimeError"
        assert "job not found" in cap["exc_msg"]
        assert cap["job_id"] == "ghost-job-42"


# ---------------------------------------------------------------------------
# Part 4: worker.py _pre_publish_check simhash 兜底
# ---------------------------------------------------------------------------


class TestWorkerSimhashObservabilityHook:

    def test_worker_simhash_check_failure_captured(self, monkeypatch):
        """similarity_checker 抛 → capture(scope='worker.simhash_check') 且仍返回 (True, None)。

        策略：直接调 _pre_publish_check 注入抛异常的 checker，无需启 DB / session。
        """
        from ai_ops.scheduler import worker as worker_mod

        capture_calls = _install_capture(monkeypatch, worker_mod)

        # 最小构造 job + article（不需要 DB，直接 dataclass-like 对象）
        class _Job:
            id = 999
            account_id = 42

        class _Article:
            body = "正文，足够长触发 simhash 路径，不含 TAINT 词"

        def boom_checker(*, text, account_id, days, threshold):
            raise RuntimeError("simhash compute boom")

        ok, err = worker_mod._pre_publish_check(
            session=None,  # _pre_publish_check 当前实现不直接用 session
            job=_Job(),
            article=_Article(),
            similarity_checker=boom_checker,
        )

        # 关键断言一：业务语义保留——查重失败放行
        assert ok is True, "simhash 兜底必须放行，避免 dedup bug 卡运营节奏"
        assert err is None

        # 关键断言二：capture 被调用 + scope/ctx 正确
        simhash_caps = [c for c in capture_calls if c.get("scope") == "worker.simhash_check"]
        assert len(simhash_caps) == 1, f"expected 1 simhash capture, got {capture_calls}"
        cap = simhash_caps[0]
        assert cap["exc_type"] == "RuntimeError"
        assert "simhash compute boom" in cap["exc_msg"]
        assert cap["job_id"] == 999
        assert cap["account_id"] == 42
