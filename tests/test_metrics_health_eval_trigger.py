"""tests/test_metrics_health_eval_trigger.py — TD-Z3-followup-A · P0 修复回归套件。

战场：`src/ai_ops/scheduler/metrics.py:collect_one` 第 60-80 行 metric_count 判定。

背景（P0 风险）：
  上一轮 TD-Z3 让 worker 在 publish 成功后立刻落第一份 Metrics（initial 行）。
  老的触发节点 `if metric_count == 2` 假设 "第 1 条 = 1h 飞轮、第 2 条 = 24h 飞轮"，
  接入 initial 行后变成 "第 1 条 = initial、第 2 条 = 1h 飞轮"——
  24h 健康度评估会在 publish + 1h 就提前 23h 触发，刚发布 1h 自然数据少，
  100% 会被 health_monitor 误判 LOW_VIEW_RATIO → DEGRADED + pause 48h。

修复方案（A）：
  metric_count 的查询条件加 `Metrics.collected_at > finished_at + 30min` 过滤，
  把 initial 行（collected_at ≈ finished_at）排除掉，只数飞轮采集；
  30min 阈值留出调度抖动余地（1h 飞轮可能 ±10 分钟跑）。

测试契约（4 条）：
  1. initial + 1h 飞轮 → metric_count == 1，不触发 evaluate_after_metrics
  2. initial + 1h + 24h 飞轮 → metric_count == 2，触发 evaluate_after_metrics
  3. 无 initial 路径（zhihu / wechat_mp 还没接入 initial_metadata） → 1h + 24h 飞轮
     → metric_count == 2，触发（向后兼容）
  4. 调度抖动：1h 飞轮提前 10 分钟跑（finished_at + 50min） → 仍正确计 1 次飞轮，不误触发

session 模板：沿用 P9 上轮收口的"单一信任源" SessionLocal.configure(bind=engine)，
production-safe（expire_on_commit=False）必须生效。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
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
from ai_ops.scheduler import metrics as metrics_mod


# ---------------------------------------------------------------------------
# Fixture：rebind 生产 SessionLocal 到 in-memory engine（与 test_initial_metrics 同套路）
# ---------------------------------------------------------------------------


@pytest.fixture
def production_session_in_memory():
    """rebind 生产 SessionLocal 到 in-memory engine。

    不另起临时 sessionmaker——保留 production kwargs（expire_on_commit=False）。
    这是 P9 上轮收口的单一信任源约定。
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


def _mk_published_job(SessionLocal, *, finished_at: datetime) -> int:
    """构造一个已发布的 job + 必要外键链路。返回 job_id。

    job.finished_at 必须显式设——它是 metric_count cutoff 锚点。
    publisher 给 platform_post_id 让 collect_one 不走 skip 分支。
    """
    with SessionLocal() as s:
        topic = Topic(name=f"t_health_eval_{id(s)}", keywords=[], persona={}, target_platforms=[])
        s.add(topic)
        s.flush()
        acc = Account(
            platform=Platform.XIAOHONGSHU,
            nickname="acc_health_eval",
            health=AccountHealth.HEALTHY,
            encrypted_credential=b"",
        )
        s.add(acc)
        s.flush()
        article = Article(
            topic_id=topic.id,
            title="health eval trigger test",
            body="body",
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
            platform_post_id="post_abc",
            platform_url="http://example.com/post_abc",
            started_at=finished_at - timedelta(minutes=2),
            finished_at=finished_at,
        )
        s.add(job)
        s.commit()
        return job.id


def _seed_metric(SessionLocal, job_id: int, *, collected_at: datetime, views: int = 100) -> None:
    """预塞一条已采集的 Metrics 行，模拟历史采集事件。

    collected_at 必须显式——本套件的核心测的就是 cutoff 与 collected_at 的关系，
    不能依赖默认 _now()。
    """
    with SessionLocal() as s:
        s.add(Metrics(
            job_id=job_id,
            likes=int(views * 0.05),
            comments=int(views * 0.01),
            shares=0,
            views=views,
            collected_at=collected_at,
            raw={},
        ))
        s.commit()


def _patch_collect_one_externals(monkeypatch, *, collected_views: int = 200):
    """统一桩掉 collect_one 外部依赖（凭证 / publisher / 热度刷新）。

    只关注 metric_count 触发节点是否调 evaluate_after_metrics——
    其他副作用（health_eval / heat_refresh）都桩成可控行为。
    """
    monkeypatch.setattr(metrics_mod, "get_credential", lambda s, aid: {"fake": "cred"})

    async def fake_collect(post_id, post_url, cred):
        # 本次采集要写入的飞轮 metric 数据
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

    # heat_refresh 不抛、不干扰
    import ai_ops.content.heat_engine as he_mod
    monkeypatch.setattr(he_mod, "recompute_topic_heat_for_article", lambda aid: None)


def _install_eval_spy(monkeypatch):
    """spy evaluate_after_metrics：返回 healthy 动作 + 记录 call_count。

    注意 metrics.py:84 是局部 import `from ..accounts.health_monitor import evaluate_after_metrics`，
    所以必须 patch 源头模块属性。
    """
    from types import SimpleNamespace
    import ai_ops.accounts.health_monitor as hm_mod

    calls: list[int] = []

    def spy(s, jid):
        calls.append(jid)
        return SimpleNamespace(decision="healthy", reason="spy")

    monkeypatch.setattr(hm_mod, "evaluate_after_metrics", spy)
    return calls


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------


class TestMetricCountCutoff:
    """metric_count cutoff filter 行为契约。"""

    def test_24h_eval_NOT_triggered_at_real_1h_with_initial_present(
        self, production_session_in_memory, monkeypatch
    ):
        """initial + 1h 飞轮场景：metric_count 应只数到 1（飞轮），不触发 health_eval。

        P0 修复的核心契约——避免发布后 1h 就误判 24h 节点导致误降权。
        时间线：
          - finished_at = T
          - initial 行 collected_at = T（worker 落库瞬间，约等于 finished_at）
          - 飞轮 1h 行 collected_at = T + 60min（collect_one 调用瞬间）
          - cutoff = T + 30min → 只数飞轮行 → count == 1 → 不触发
        """
        SessionLocal = production_session_in_memory
        now = datetime.utcnow()
        finished_at = now - timedelta(hours=1, minutes=2)  # publish 完成 ≈ 62 分钟前
        job_id = _mk_published_job(SessionLocal, finished_at=finished_at)

        # 预塞 initial 行：collected_at ≈ finished_at（worker._persist_initial_metrics 实际行为）
        _seed_metric(SessionLocal, job_id, collected_at=finished_at, views=50)

        _patch_collect_one_externals(monkeypatch, collected_views=200)
        eval_calls = _install_eval_spy(monkeypatch)

        # 跑 collect_one 模拟 1h 飞轮（collected_at = now ≈ finished_at + 62min）
        asyncio.run(metrics_mod.collect_one(job_id))

        # 关键断言：health_eval 不应触发
        assert eval_calls == [], (
            f"P0 回归：1h 飞轮节点 health_eval 被误触发，调用记录 {eval_calls}"
        )

        # 副验证：Metrics 表确实有 2 行（initial + 飞轮），但 cutoff filter 后只数 1 行
        with SessionLocal() as s:
            total = s.query(Metrics).filter(Metrics.job_id == job_id).count()
            assert total == 2, f"实际有 2 条 metric（initial + 飞轮），实际 {total}"
            cutoff = finished_at + timedelta(minutes=30)
            counted = (
                s.query(Metrics)
                .filter(Metrics.job_id == job_id, Metrics.collected_at > cutoff)
                .count()
            )
            assert counted == 1, f"cutoff 后应只数到飞轮 1 条，实际 {counted}"

    def test_24h_eval_triggered_when_24h_metric_arrives_with_initial(
        self, production_session_in_memory, monkeypatch
    ):
        """initial + 1h 飞轮 + 24h 飞轮场景：metric_count 应等于 2（两次飞轮），触发 health_eval。

        24h 节点的真实触发：cutoff 后第 2 条飞轮 metric 落库时。
        时间线：
          - finished_at = T-25h
          - initial 行 collected_at = T-25h
          - 1h 飞轮 collected_at = T-24h（预塞）
          - 24h 飞轮 collected_at = T（本次 collect_one）
          - cutoff = T-24.5h → 数到 1h + 24h 两条 → count == 2 → 触发
        """
        SessionLocal = production_session_in_memory
        now = datetime.utcnow()
        finished_at = now - timedelta(hours=25)
        job_id = _mk_published_job(SessionLocal, finished_at=finished_at)

        # initial 行
        _seed_metric(SessionLocal, job_id, collected_at=finished_at, views=50)
        # 1h 飞轮已采过
        _seed_metric(SessionLocal, job_id, collected_at=finished_at + timedelta(hours=1), views=300)

        _patch_collect_one_externals(monkeypatch, collected_views=1500)
        eval_calls = _install_eval_spy(monkeypatch)

        # 跑 24h 飞轮采集（collected_at = now ≈ finished_at + 25h）
        asyncio.run(metrics_mod.collect_one(job_id))

        # 关键断言：health_eval 应触发一次
        assert eval_calls == [job_id], (
            f"24h 节点 health_eval 未触发或多次触发，调用记录 {eval_calls}"
        )

    def test_24h_eval_triggered_backward_compat_no_initial(
        self, production_session_in_memory, monkeypatch
    ):
        """向后兼容：无 initial 行的旧路径（zhihu / wechat_mp 还没接入 initial_metadata）。

        时间线（无 initial）：
          - finished_at = T-25h
          - 1h 飞轮 collected_at = T-24h（预塞）
          - 24h 飞轮 collected_at = T（本次 collect_one）
          - cutoff = T-24.5h → 数到 1h + 24h 两条 → count == 2 → 触发
        """
        SessionLocal = production_session_in_memory
        now = datetime.utcnow()
        finished_at = now - timedelta(hours=25)
        job_id = _mk_published_job(SessionLocal, finished_at=finished_at)

        # 注意：不预塞 initial 行 —— 模拟没接入 _persist_initial_metrics 的 publisher 路径
        # 1h 飞轮已采过
        _seed_metric(SessionLocal, job_id, collected_at=finished_at + timedelta(hours=1), views=300)

        _patch_collect_one_externals(monkeypatch, collected_views=1500)
        eval_calls = _install_eval_spy(monkeypatch)

        # 跑 24h 飞轮采集
        asyncio.run(metrics_mod.collect_one(job_id))

        # 关键断言：旧路径下行为不变，health_eval 仍正常触发
        assert eval_calls == [job_id], (
            f"无 initial 路径行为不应被破坏，health_eval 调用记录 {eval_calls}"
        )

    def test_30min_cutoff_handles_scheduler_jitter(
        self, production_session_in_memory, monkeypatch
    ):
        """调度抖动：1h 飞轮提前 10 分钟跑（finished_at + 50min），仍能正确计数为飞轮。

        30min cutoff 设计的余地正是为了这种抖动：
          - finished_at = T-50min
          - initial 行 collected_at = T-50min
          - 1h 飞轮提前到 collected_at = T（finished_at + 50min，正在比预期早 10min）
          - cutoff = T-20min → 飞轮 metric (T > T-20min) 仍在 cutoff 内 → count == 1
          - initial metric (T-50min < T-20min) 仍被排除 → 不误触发
        """
        SessionLocal = production_session_in_memory
        now = datetime.utcnow()
        finished_at = now - timedelta(minutes=50)  # 50 分钟前发布
        job_id = _mk_published_job(SessionLocal, finished_at=finished_at)

        # initial 行
        _seed_metric(SessionLocal, job_id, collected_at=finished_at, views=50)

        _patch_collect_one_externals(monkeypatch, collected_views=200)
        eval_calls = _install_eval_spy(monkeypatch)

        # 跑 collect_one 模拟"提前 10 分钟跑的 1h 飞轮"（实际 collected_at = now ≈ finished_at + 50min）
        asyncio.run(metrics_mod.collect_one(job_id))

        # 关键断言：抖动不引入误触发；飞轮 metric 仍正确计为 1 条
        assert eval_calls == [], (
            f"30min 抖动余地失效，1h 飞轮被误判为 24h 节点，调用记录 {eval_calls}"
        )
        # 副验证：cutoff filter 后确实数到了飞轮那一条
        with SessionLocal() as s:
            cutoff = finished_at + timedelta(minutes=30)
            counted = (
                s.query(Metrics)
                .filter(Metrics.job_id == job_id, Metrics.collected_at > cutoff)
                .count()
            )
            assert counted == 1, f"飞轮 metric 应在 cutoff 之后被计入，实际 count={counted}"
