"""FakeApplier —— 离线/演示用的假适配器。

价值：让 matcher / pipeline / 候选池 / CLI 在**完全不碰真平台**的情况下端到端可跑可测。
真 Boss 登录态就绪前（P2），`jobhunt crawl-match --fake` 即可演练整条管道。
"""
from __future__ import annotations

from ..enums import JobBoard
from ..schemas import ApplyResult, JobCandidate, JobQuery
from .base import ApplierBase

# 一小批固定岗位（贴近真实 JD 文本，让 matcher 打分有东西可咬）
_FIXTURE_JOBS = [
    JobCandidate(
        board=JobBoard.BOSS,
        external_id="fake-001",
        url="https://example.com/job/fake-001",
        title="高级后端工程师（Go）",
        company="示例科技",
        location="杭州",
        salary_text="30-50K·14薪",
        jd_text=(
            "负责核心交易系统后端研发；要求 5 年以上 Go 经验，"
            "熟悉 Kubernetes、微服务、高并发；有日活千万级系统经验优先。"
        ),
        tags=["Go", "Kubernetes", "微服务"],
    ),
    JobCandidate(
        board=JobBoard.BOSS,
        external_id="fake-002",
        url="https://example.com/job/fake-002",
        title="Python 数据开发工程师",
        company="示例数据",
        location="上海",
        salary_text="20-35K",
        jd_text="负责数据管道开发；要求熟悉 Python、Spark、数仓建模；3 年以上经验。",
        tags=["Python", "Spark", "数仓"],
    ),
    JobCandidate(
        board=JobBoard.BOSS,
        external_id="fake-003",
        url="https://example.com/job/fake-003",
        title="前端工程师（React）",
        company="示例前端",
        location="深圳",
        salary_text="20-30K",
        jd_text="负责 Web 前端开发；要求精通 React、TypeScript；与后端协作。",
        tags=["React", "TypeScript"],
    ),
]


class FakeApplier(ApplierBase):
    board = JobBoard.BOSS

    def __init__(self, jobs: list[JobCandidate] | None = None):
        self._jobs = jobs if jobs is not None else list(_FIXTURE_JOBS)

    async def search_jobs(self, query: JobQuery, *, credential: dict | None = None) -> list[JobCandidate]:
        # 简单按关键词过滤（命中标题/JD/标签任一即可），再截断到 limit
        kws = [k.lower() for k in query.keywords]
        if not kws:
            hits = self._jobs
        else:
            hits = [
                j for j in self._jobs
                if any(
                    k in (j.title + j.jd_text + " ".join(j.tags)).lower()
                    for k in kws
                )
            ]
        return hits[: query.limit]

    async def apply(self, *, credential: dict, job: JobCandidate, resume_summary: str, greeting: str) -> ApplyResult:
        # 假投递：永远成功，回显 greeting 长度便于断言
        return ApplyResult(
            success=True,
            external_id=f"applied-{job.external_id}",
            url=job.url,
            raw={"greeting_len": len(greeting)},
        )
