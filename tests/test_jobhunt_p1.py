"""jobhunt 专题 P1 验收测试 —— 采集 → 匹配打分 → 落候选池(DRAFT)。

纯离线、确定性：Applier 用 FakeApplier（固定岗位），LLM 用注入的 fake driver。
覆盖：
  1. matcher：fake LLM → MatchResult，score 夹取、verdict 分档、非法 JSON 报错
  2. greeting：长度护栏、去引号
  3. registry：按 JobBoard 路由 + 优先级 + 未注册报错
  4. pipeline：端到端落 Application(DRAFT) + JobMatch + JobPosting upsert
     + 阈值过滤 + 去重幂等（再跑一次不重复落）
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from ai_ops.core.models import Base
from ai_ops.jobhunt import models as jh_models  # noqa: F401  注册四表
from ai_ops.jobhunt.appliers.base import ApplierBase
from ai_ops.jobhunt.appliers.fake import FakeApplier
from ai_ops.jobhunt.appliers.registry import ApplierRegistry
from ai_ops.jobhunt.enums import ApplicationStatus, JobBoard, MatchVerdict
from ai_ops.jobhunt.greeting import GreetingGenerator
from ai_ops.jobhunt.matcher import JobMatcher, _verdict_from_score
from ai_ops.jobhunt.models import Application, JobMatch, JobPosting, ResumeProfile
from ai_ops.jobhunt.pipeline import JobHuntPipeline
from ai_ops.jobhunt.schemas import JobCandidate, JobQuery


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def resume(session):
    r = ResumeProfile(
        name="后端-2026",
        raw_text="",
        structured={
            "summary": "6 年 Go/Python 后端",
            "skills": ["Go", "Python", "Kubernetes"],
            "years_of_experience": 6,
        },
        search_keywords=["Go", "Python"],
        expected_cities=["杭州"],
        target_titles=["后端工程师"],
        is_active=True,
    )
    session.add(r)
    session.flush()
    return r


class ScriptedDriver:
    """按调用次序返回预设响应的假 LLM；matcher 与 greeting 共用。"""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls = 0

    async def complete(self, system: str, user: str, **kw) -> str:
        i = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[i]


def _match_json(score: int) -> str:
    return (
        '```json\n{"score": %d, "matched_points": ["Go 6 年", "K8s"], '
        '"gaps": ["缺 Rust"], "reasoning": "技能高度吻合"}\n```' % score
    )


# ---------------------------------------------------------------------------
# 1) matcher
# ---------------------------------------------------------------------------
def test_verdict_bands():
    assert _verdict_from_score(80) == MatchVerdict.STRONG
    assert _verdict_from_score(60) == MatchVerdict.MAYBE
    assert _verdict_from_score(40) == MatchVerdict.WEAK


async def test_matcher_parses_and_bands():
    m = JobMatcher(ScriptedDriver([_match_json(82)]))
    job = JobCandidate(board=JobBoard.BOSS, external_id="j1", title="后端", jd_text="Go 微服务")
    res = await m.score({"skills": ["Go"]}, job)
    assert res.score == 82.0
    assert res.verdict == MatchVerdict.STRONG
    assert "Go 6 年" in res.matched_points
    assert res.gaps == ["缺 Rust"]


async def test_matcher_clamps_out_of_range_score():
    m = JobMatcher(ScriptedDriver(['{"score": 999, "reasoning": "x"}']))
    job = JobCandidate(board=JobBoard.BOSS, external_id="j1")
    res = await m.score({}, job)
    assert res.score == 100.0  # 夹到上限


async def test_matcher_bad_json_raises():
    m = JobMatcher(ScriptedDriver(["抱歉无法评估"]))
    job = JobCandidate(board=JobBoard.BOSS, external_id="j1")
    with pytest.raises(ValueError, match="非法 JSON"):
        await m.score({}, job)


# ---------------------------------------------------------------------------
# 2) greeting
# ---------------------------------------------------------------------------
async def test_greeting_strips_quotes_and_caps_length():
    long_text = '"' + ("你好" * 200) + '"'
    g = GreetingGenerator(ScriptedDriver([long_text]))
    job = JobCandidate(board=JobBoard.BOSS, external_id="j1", title="后端", company="X")
    out = await g.generate({"skills": ["Go"]}, job, None)
    assert not out.startswith('"')      # 引号被去掉
    assert len(out) <= 220              # 长度护栏


# ---------------------------------------------------------------------------
# 3) registry
# ---------------------------------------------------------------------------
def test_registry_priority_and_missing():
    reg = ApplierRegistry()

    class A(FakeApplier):
        board = JobBoard.BOSS

    class B(FakeApplier):
        board = JobBoard.BOSS

    reg.register(JobBoard.BOSS, B, priority=20)
    reg.register(JobBoard.BOSS, A, priority=10)
    assert isinstance(reg.first(JobBoard.BOSS), A)         # 优先级小的先
    assert len(reg.resolve(JobBoard.BOSS)) == 2
    with pytest.raises(ValueError, match="未注册"):
        reg.first(JobBoard.ZHILIAN)


def test_default_registry_has_boss():
    from ai_ops.jobhunt.appliers.registry import default_registry
    from ai_ops.jobhunt.appliers.boss import BossApplier
    assert isinstance(default_registry.first(JobBoard.BOSS), BossApplier)


# ---------------------------------------------------------------------------
# 4) pipeline 端到端
# ---------------------------------------------------------------------------
def _pipeline_with_scores(scores: list[int]) -> JobHuntPipeline:
    """每个岗位先 matcher（吃一条 match json），过阈值再 greeting（吃一条招呼）。
    用足够多的 greeting 兜底响应，避免次序错位。"""
    match_resps = [_match_json(s) for s in scores]
    # matcher 和 greeting 各自独立 driver，避免共享次序耦合
    matcher = JobMatcher(ScriptedDriver(match_resps))
    greeter = GreetingGenerator(ScriptedDriver(["你好，我很匹配这个岗位，方便详聊吗？"]))
    return JobHuntPipeline(FakeApplier(), matcher, greeter)


async def test_pipeline_stages_only_passing(session, resume):
    # FakeApplier 固定 3 个岗位；给分 [80, 50, 70]，阈值 60 → 落 2 个（80、70）
    pipe = _pipeline_with_scores([80, 50, 70])
    out = await pipe.run(session, resume, JobQuery(keywords=[""], limit=10), min_score=60)

    assert out.searched == 3
    assert out.scored == 3
    assert out.staged == 2
    assert out.skipped_below == 1

    apps = session.scalars(
        select(Application).where(Application.status == ApplicationStatus.DRAFT)
    ).all()
    assert len(apps) == 2
    # 每条候选都带招呼语 + 关联 match
    for a in apps:
        assert a.greeting
        assert a.match_id is not None
    # JobMatch 三条全落（含没过阈值的，留作"为什么没投"依据）
    assert len(session.scalars(select(JobMatch)).all()) == 3
    # JobPosting upsert 出 3 条
    assert len(session.scalars(select(JobPosting)).all()) == 3


async def test_pipeline_idempotent_no_dup(session, resume):
    """再跑一次：已在候选池的岗位被 skipped_dup，不重复落、不重复打分。"""
    out1 = await _pipeline_with_scores([80, 50, 70]).run(
        session, resume, JobQuery(keywords=[""], limit=10), min_score=60
    )
    assert out1.staged == 2

    # 第二轮：已落候选池的 2 个在打分前就被 skipped_dup；唯一没进池的（fake-002）
    # 会被"重新考虑"（合理：JD 可能更新）。给它低分 → 仍不过阈值，不新增。
    out2 = await _pipeline_with_scores([50, 50, 50]).run(
        session, resume, JobQuery(keywords=[""], limit=10), min_score=60
    )
    assert out2.skipped_dup == 2      # 已在池的两个，跳过且不重复打分
    assert out2.scored == 1           # 只有未入池的那个被重新打分
    assert out2.staged == 0           # 低分，不新增
    assert out2.skipped_below == 1
    # 候选池总数仍是 2（幂等，无重复）
    assert len(session.scalars(select(Application)).all()) == 2


async def test_fake_applier_keyword_filter():
    fa = FakeApplier()
    # 关键词 React 只命中前端那条
    hits = await fa.search_jobs(JobQuery(keywords=["React"], limit=10))
    assert len(hits) == 1 and "前端" in hits[0].title
    # 空关键词返回全部
    assert len(await fa.search_jobs(JobQuery(keywords=[], limit=10))) == 3


async def test_applier_base_is_abstract():
    with pytest.raises(TypeError):
        ApplierBase()  # 抽象类不可实例化
