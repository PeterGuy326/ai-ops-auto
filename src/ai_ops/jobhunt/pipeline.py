"""JobHuntPipeline —— 采集 → 匹配打分 → 落候选池(DRAFT)。

对齐 content/distributor 的「不直投，先落 DRAFT 等人工勾选」哲学：
本管道只把过阈值的岗位落成 Application(status=DRAFT)，**绝不真投**——
真投递（APPLIED）由 P2 的 execute 接 scheduler 风控后才发生。

一次 run 的内部流水（每个岗位独立走）：
  applier.search_jobs → upsert JobPosting → matcher.score → upsert JobMatch
    └─ score ≥ min_score 且未投过 → greeting.generate → Application(DRAFT)

去重两层：
  - JobPosting 按 (board, external_id) upsert，重复采集不造重复岗位
  - Application 按 (resume_id, job_id) 去重，同岗位不重复进候选池
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .appliers.base import ApplierBase
from .enums import ApplicationStatus
from .greeting import GreetingGenerator
from .matcher import JobMatcher
from .models import Application, JobMatch, JobPosting, ResumeProfile
from .schemas import JobCandidate, JobQuery, MatchResult


@dataclass
class CrawlMatchResult:
    """一次 crawl-match 的战报。"""
    searched: int = 0          # 采集到的岗位数
    scored: int = 0            # 打分数
    staged: int = 0            # 新落候选池数
    skipped_below: int = 0     # 分数不够被过滤
    skipped_dup: int = 0       # 已投过/已在候选池
    staged_ids: list[int] = field(default_factory=list)


class JobHuntPipeline:
    def __init__(
        self,
        applier: ApplierBase,
        matcher: JobMatcher | None = None,
        greeting_gen: GreetingGenerator | None = None,
    ):
        self.applier = applier
        self.matcher = matcher or JobMatcher()
        self.greeting_gen = greeting_gen or GreetingGenerator()

    async def run(
        self,
        session: Session,
        resume: ResumeProfile,
        query: JobQuery | None = None,
        *,
        credential: dict | None = None,
        min_score: float = 60.0,
    ) -> CrawlMatchResult:
        if query is None:
            query = JobQuery(
                keywords=list(resume.search_keywords or resume.target_titles or []),
                city=(resume.expected_cities or [""])[0],
                salary_min=resume.expected_salary_min,
            )

        candidates = await self.applier.search_jobs(query, credential=credential)
        out = CrawlMatchResult(searched=len(candidates))

        for cand in candidates:
            job = self._upsert_job(session, cand)

            # 已在候选池/已投过 → 跳过（省掉打分的 LLM 开销）
            if self._application_exists(session, resume.id, job.id):
                out.skipped_dup += 1
                continue

            match = await self.matcher.score(resume.structured or {}, cand)
            out.scored += 1
            self._upsert_match(session, resume.id, job.id, match)

            if match.score < min_score:
                out.skipped_below += 1
                continue

            greeting = await self.greeting_gen.generate(resume.structured or {}, cand, match)
            app = Application(
                resume_id=resume.id,
                job_id=job.id,
                match_id=self._match_id(session, resume.id, job.id),
                board=cand.board,
                status=ApplicationStatus.DRAFT,
                greeting=greeting,
            )
            session.add(app)
            session.flush()
            out.staged += 1
            out.staged_ids.append(app.id)

        return out

    # ------------------------------------------------------------------
    # upsert / 去重 helpers
    # ------------------------------------------------------------------
    def _upsert_job(self, session: Session, cand: JobCandidate) -> JobPosting:
        job = session.scalar(
            select(JobPosting).where(
                JobPosting.board == cand.board,
                JobPosting.external_id == cand.external_id,
            )
        )
        if job is None:
            job = JobPosting(board=cand.board, external_id=cand.external_id)
            session.add(job)
        # 每次采集刷新可变字段（薪资/JD 可能变）
        job.url = cand.url
        job.title = cand.title
        job.company = cand.company
        job.location = cand.location
        job.salary_text = cand.salary_text
        job.jd_text = cand.jd_text
        job.tags = list(cand.tags)
        job.raw = dict(cand.raw)
        session.flush()
        return job

    def _upsert_match(self, session: Session, resume_id: int, job_id: int, m: MatchResult) -> JobMatch:
        match = session.scalar(
            select(JobMatch).where(JobMatch.resume_id == resume_id, JobMatch.job_id == job_id)
        )
        if match is None:
            match = JobMatch(resume_id=resume_id, job_id=job_id)
            session.add(match)
        match.score = m.score
        match.verdict = m.verdict.value
        match.matched_points = list(m.matched_points)
        match.gaps = list(m.gaps)
        match.reasoning = m.reasoning
        match.model = m.model
        session.flush()
        return match

    @staticmethod
    def _application_exists(session: Session, resume_id: int, job_id: int) -> bool:
        return session.scalar(
            select(Application.id).where(
                Application.resume_id == resume_id, Application.job_id == job_id
            )
        ) is not None

    @staticmethod
    def _match_id(session: Session, resume_id: int, job_id: int) -> int | None:
        return session.scalar(
            select(JobMatch.id).where(JobMatch.resume_id == resume_id, JobMatch.job_id == job_id)
        )
