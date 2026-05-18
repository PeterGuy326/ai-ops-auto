"""tests/test_metrics_interval_index.py — TD-P0-debt2 · interval_index 显式化回归套件。

战场：`src/ai_ops/scheduler/metrics.py:collect_one` 触发判定 + `schedule_after_publish` 闭包。

背景（上一轮 P0 修一半）：
  TD-Z3-followup-A 把 24h 节点判定从裸 `metric_count == 2` 改成"cutoff + count"，
  解决了 worker 写 initial 行后的提前 23h 误触发 P0；但仍隐含
  "第 2 个飞轮节点 = 24h"——如果未来给 DEFAULT_INTERVALS_SECONDS 加 30min 实时档位
  （如 (1800, 3600, 86400, 604800)），第 2 个就变成 1h 节点，P0 再次触发。

本轮 owner 修法：
  把"我是第几档飞轮"从隐式从 metric_count 推断升级成显式 interval_index 入参，
  配合 HEALTH_EVAL_INTERVAL_INDEX 常量定义"哪个 index 是 24h 节点"。
  - interval_index=N（显式飞轮调度路径）→ 直接 `N == HEALTH_EVAL_INTERVAL_INDEX` 判定
  - interval_index=None（手动触发 / 老调用方）→ 保留 cutoff + count 兼容路径

测试契约（≥ 5 用例 + 1 bonus）：
  1. 显式 interval_index=HEALTH_EVAL_INTERVAL_INDEX → 触发 health_eval，与表中 metric 总数无关
  2. 显式 interval_index=0 → 不触发（不是 24h 节点）
  3. 显式 interval_index=2 → 不触发（7d 节点，不是 24h）
  4. interval_index=None → 走 cutoff + count 兼容路径（向后兼容守护）
  5. schedule_after_publish enumerate 闭包：3 次调度分别传 i=0/1/2 给 callback
     （防 Python late-binding 经典 bug：for 内 lambda 不用默认参数会全捕获最后一次 idx）
  6. [bonus] HEALTH_EVAL_INTERVAL_INDEX 常量约束：必须在 DEFAULT_INTERVALS_SECONDS 长度内
     （改飞轮档位时同步守护）

session 模板：沿用 P9 上轮收口的"单一信任源" SessionLocal.configure(bind=engine)，
production-safe（expire_on_commit=False）必须生效。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
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
# Fixture：rebind 生产 SessionLocal 到 in-memory engine
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
    """构造一个已发布的 job + 必要外键链路。返回 job_id。"""
    with SessionLocal() as s:
        topic = Topic(
            name=f"t_interval_idx_{id(s)}",
            keywords=[],
            persona={},
            target_platforms=[],
        )
        s.add(topic)
        s.flush()
        acc = Account(
            platform=Platform.XIAOHONGSHU,
            nickname="acc_interval_idx",
            health=AccountHealth.HEALTHY,
            encrypted_credential=b"",
        )
        s.add(acc)
        s.flush()
        article = Article(
            topic_id=topic.id,
            title="interval idx test",
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
            platform_post_id="post_idx",
            platform_url="http://example.com/post_idx",
            started_at=finished_at - timedelta(minutes=2),
            finished_at=finished_at,
        )
        s.add(job)
        s.commit()
        return job.id


def _seed_metric(
    SessionLocal,
    job_id: int,
    *,
    collected_at: datetime,
    views: int = 100,
) -> None:
    """预塞一条已采集的 Metrics 行，模拟历史采集事件。"""
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
    """统一桩掉 collect_one 外部依赖（凭证 / publisher / 热度刷新）。"""
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

    # heat_refresh 不抛、不干扰
    import ai_ops.content.heat_engine as he_mod
    monkeypatch.setattr(he_mod, "recompute_topic_heat_for_article", lambda aid: None)


def _install_eval_spy(monkeypatch):
    """spy evaluate_after_metrics：返回 healthy 动作 + 记录 call_count。

    注意 metrics.py 的 evaluate_after_metrics 是局部 import,
    必须 patch 源头模块属性（ai_ops.accounts.health_monitor）。
    """
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


class TestIntervalIndexExplicitPath:
    """显式 interval_index 路径——飞轮调度真实走的路径。"""

    def test_interval_index_health_node_triggers_eval_regardless_of_count(
        self, production_session_in_memory, monkeypatch
    ):
        """显式传 HEALTH_EVAL_INTERVAL_INDEX → 触发 health_eval，与表中 metric 总数无关。

        owner 修法核心契约：触发判定不再依赖"恰好第 N 条 metric 落库"。
        本用例预塞 99 条 metric 制造极端噪声，证明显式路径完全无视 count。
        """
        SessionLocal = production_session_in_memory
        now = datetime.utcnow()
        finished_at = now - timedelta(hours=1)
        job_id = _mk_published_job(SessionLocal, finished_at=finished_at)

        # 极端噪声：预塞 99 条 metric。如果还走 metric_count == 2 路径会完全不触发；
        # 显式路径必须无视它直接触发
        for i in range(99):
            _seed_metric(
                SessionLocal,
                job_id,
                collected_at=finished_at + timedelta(minutes=40 + i),
                views=10,
            )

        _patch_collect_one_externals(monkeypatch, collected_views=200)
        eval_calls = _install_eval_spy(monkeypatch)

        asyncio.run(metrics_mod.collect_one(
            job_id,
            interval_index=metrics_mod.HEALTH_EVAL_INTERVAL_INDEX,
        ))

        assert eval_calls == [job_id], (
            f"显式 interval_index=HEALTH_EVAL_INTERVAL_INDEX 必须触发 health_eval，"
            f"实际调用 {eval_calls}（如为空说明 count 路径污染了显式路径判定）"
        )

    def test_interval_index_health_node_triggers_even_with_zero_existing(
        self, production_session_in_memory, monkeypatch
    ):
        """显式传 HEALTH_EVAL_INTERVAL_INDEX → 触发，即使表里 0 条历史 metric。

        反向证明：count == 0 在老路径下绝不触发，但显式路径必须触发。
        模拟"1h 飞轮被跳过、直接跑 24h"的极端调度场景。
        """
        SessionLocal = production_session_in_memory
        now = datetime.utcnow()
        finished_at = now - timedelta(hours=24)
        job_id = _mk_published_job(SessionLocal, finished_at=finished_at)

        # 不预塞任何 metric

        _patch_collect_one_externals(monkeypatch, collected_views=1500)
        eval_calls = _install_eval_spy(monkeypatch)

        asyncio.run(metrics_mod.collect_one(
            job_id,
            interval_index=metrics_mod.HEALTH_EVAL_INTERVAL_INDEX,
        ))

        assert eval_calls == [job_id], (
            f"显式路径 + 表里 0 条历史也应触发，实际 {eval_calls}"
        )

    def test_interval_index_0_does_not_trigger(
        self, production_session_in_memory, monkeypatch
    ):
        """显式 interval_index=0（1h 节点）不触发 health_eval。"""
        SessionLocal = production_session_in_memory
        now = datetime.utcnow()
        finished_at = now - timedelta(hours=1)
        job_id = _mk_published_job(SessionLocal, finished_at=finished_at)

        _patch_collect_one_externals(monkeypatch, collected_views=200)
        eval_calls = _install_eval_spy(monkeypatch)

        asyncio.run(metrics_mod.collect_one(job_id, interval_index=0))

        assert eval_calls == [], (
            f"interval_index=0 是 1h 节点，绝不应触发 health_eval，实际 {eval_calls}"
        )

    def test_interval_index_2_does_not_trigger(
        self, production_session_in_memory, monkeypatch
    ):
        """显式 interval_index=2（7d 节点）不触发 health_eval。"""
        SessionLocal = production_session_in_memory
        now = datetime.utcnow()
        finished_at = now - timedelta(days=7)
        job_id = _mk_published_job(SessionLocal, finished_at=finished_at)

        # 预塞 1h + 24h 飞轮已跑过的 metric——证明显式路径不会因为"恰好第 3 条"
        # 而被旧 count 逻辑兜底误触发
        _seed_metric(SessionLocal, job_id, collected_at=finished_at + timedelta(hours=1))
        _seed_metric(SessionLocal, job_id, collected_at=finished_at + timedelta(hours=24))

        _patch_collect_one_externals(monkeypatch, collected_views=5000)
        eval_calls = _install_eval_spy(monkeypatch)

        asyncio.run(metrics_mod.collect_one(job_id, interval_index=2))

        assert eval_calls == [], (
            f"interval_index=2 是 7d 节点，绝不应触发 health_eval，实际 {eval_calls}"
        )


class TestIntervalIndexNoneBackwardCompat:
    """interval_index=None 兼容路径——手动触发 / 老调用方 / observability 测试。"""

    def test_interval_index_none_falls_back_to_count_triggers(
        self, production_session_in_memory, monkeypatch
    ):
        """不传 interval_index → 走 cutoff + count 路径。

        构造经典 24h 场景（1h + 24h 飞轮各 1 条）让 count == 2 触发，
        证明兼容路径行为完全不变（与 test_metrics_health_eval_trigger.py 第 2 用例同义）。
        """
        SessionLocal = production_session_in_memory
        now = datetime.utcnow()
        finished_at = now - timedelta(hours=25)
        job_id = _mk_published_job(SessionLocal, finished_at=finished_at)

        _seed_metric(SessionLocal, job_id, collected_at=finished_at, views=50)  # initial
        _seed_metric(SessionLocal, job_id, collected_at=finished_at + timedelta(hours=1), views=300)  # 1h 飞轮

        _patch_collect_one_externals(monkeypatch, collected_views=1500)
        eval_calls = _install_eval_spy(monkeypatch)

        # 不传 interval_index → 兼容路径
        asyncio.run(metrics_mod.collect_one(job_id))

        assert eval_calls == [job_id], (
            f"interval_index=None 兼容路径必须保留 cutoff+count 行为，"
            f"实际 {eval_calls}"
        )

    def test_interval_index_none_falls_back_to_count_no_trigger(
        self, production_session_in_memory, monkeypatch
    ):
        """不传 interval_index + count < 2 → 不触发。

        反向守护：兼容路径在非触发条件下也行为正确（防"改完触发太松"）。
        """
        SessionLocal = production_session_in_memory
        now = datetime.utcnow()
        finished_at = now - timedelta(hours=1, minutes=2)
        job_id = _mk_published_job(SessionLocal, finished_at=finished_at)

        # 只有 initial 行，cutoff 后 count == 1（本次飞轮写入），不触发
        _seed_metric(SessionLocal, job_id, collected_at=finished_at, views=50)

        _patch_collect_one_externals(monkeypatch, collected_views=200)
        eval_calls = _install_eval_spy(monkeypatch)

        asyncio.run(metrics_mod.collect_one(job_id))

        assert eval_calls == [], (
            f"兼容路径 + count == 1 不应触发，实际 {eval_calls}"
        )


class TestScheduleAfterPublishClosureCapture:
    """schedule_after_publish 闭包陷阱守护——Python late-binding 经典 bug 防线。"""

    def test_schedule_after_publish_passes_interval_index(self, monkeypatch):
        """spy queue.schedule_once，验证 3 次调度传给 callback 的 interval_index 是 0/1/2。

        如果 for 内 lambda 没用默认参数捕获 idx，3 个回调会全捕获最后一次 idx=2
        （Python late-binding 经典 bug）。本用例真实执行回调把 i 抓出来比对，
        强制证明 early-binding 生效。
        """
        captured_interval_indexes: list[int] = []
        captured_job_ids: list[int] = []

        # 让 collect_one 被回调时把 (job_id, interval_index) 抓下来；
        # 不实际跑业务，立刻返回避免 asyncio.create_task 报 no event loop
        async def spy_collect_one(jid, *, interval_index=None):
            captured_job_ids.append(jid)
            captured_interval_indexes.append(interval_index)
            return {"spy": True}

        monkeypatch.setattr(metrics_mod, "collect_one", spy_collect_one)

        # spy queue.schedule_once：记录每次 coro_factory 并立即触发它跑回调
        # 闭包内部是 lambda: asyncio.create_task(collect_one(jid, interval_index=i))
        # asyncio 是函数体内 local import 的，patch 不到模块属性——直接 patch 全局 asyncio.create_task
        registered_ids: list[str] = []

        # 替换 asyncio.create_task：同步 drive coroutine 让 spy_collect_one 抓参数
        # （否则真创建 task 会因没 event loop 报 RuntimeError）
        import asyncio as _asyncio

        def fake_create_task(coro):
            try:
                coro.send(None)
            except StopIteration:
                pass
            return None

        monkeypatch.setattr(_asyncio, "create_task", fake_create_task)

        def fake_schedule_once(when, coro_factory, job_id=None):
            registered_ids.append(job_id)
            coro_factory()  # 立即触发 lambda → spy_collect_one 被同步抓参数
            return job_id or f"sched-{len(registered_ids)}"

        monkeypatch.setattr(metrics_mod.queue, "schedule_once", fake_schedule_once)

        # 跑 schedule_after_publish
        ids = metrics_mod.schedule_after_publish(job_id=12345)

        # 必须注册 3 次（默认 3 档）
        assert len(ids) == 3, f"应注册 3 次，实际 {len(ids)}: {ids}"

        # 闭包陷阱核心断言：3 次回调的 interval_index 必须是 0, 1, 2（不是全 2）
        assert captured_interval_indexes == [0, 1, 2], (
            f"闭包 late-binding bug！期望 [0,1,2]，实际 {captured_interval_indexes}。"
            f"修法：for idx, delay in enumerate(...) 内 lambda 必须 `i=idx` 默认参数捕获。"
        )
        # job_id 也应每次都是 12345（同样的 early-binding 守护）
        assert captured_job_ids == [12345, 12345, 12345], (
            f"job_id 闭包也炸了！实际 {captured_job_ids}"
        )


class TestHealthEvalIntervalIndexConstant:
    """HEALTH_EVAL_INTERVAL_INDEX 常量约束守护——改飞轮档位时同步检查。"""

    def test_health_eval_interval_index_constant_in_bounds(self):
        """HEALTH_EVAL_INTERVAL_INDEX 必须在 DEFAULT_INTERVALS_SECONDS 长度内。

        如果未来给 DEFAULT_INTERVALS_SECONDS 增减档位，开发者必须同步改这个常量
        到对应"24h 节点"的新 index——本守护让忘记改时立刻挂测试。
        """
        assert hasattr(metrics_mod, "HEALTH_EVAL_INTERVAL_INDEX"), (
            "HEALTH_EVAL_INTERVAL_INDEX 常量缺失"
        )
        assert hasattr(metrics_mod, "DEFAULT_INTERVALS_SECONDS"), (
            "DEFAULT_INTERVALS_SECONDS 常量缺失"
        )

        idx = metrics_mod.HEALTH_EVAL_INTERVAL_INDEX
        intervals = metrics_mod.DEFAULT_INTERVALS_SECONDS

        assert isinstance(idx, int), f"HEALTH_EVAL_INTERVAL_INDEX 应为 int，实际 {type(idx)}"
        assert 0 <= idx < len(intervals), (
            f"HEALTH_EVAL_INTERVAL_INDEX={idx} 越界（intervals 长度 {len(intervals)}）。"
            f"加 / 删飞轮档位时记得同步本常量。"
        )

        # 语义守护：HEALTH_EVAL_INTERVAL_INDEX 对应的 interval 必须 = 86400 (24h)
        assert intervals[idx] == 86400, (
            f"HEALTH_EVAL_INTERVAL_INDEX={idx} 指向 intervals[{idx}]={intervals[idx]}s，"
            f"不是 86400（24h）。检查 DEFAULT_INTERVALS_SECONDS 的档位顺序是否还对应 1h/24h/7d。"
        )
