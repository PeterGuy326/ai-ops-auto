"""jobhunt 专题 DTO —— 平台无关的标准化结构（对齐 core.schemas 的 pydantic 风格）。

DTO 与 ORM（models.py）刻意分离：
  - 爬虫吐 JobCandidate（轻量、不依赖 DB session），pipeline 再 upsert 成 JobPosting(ORM)
  - matcher 吐 MatchResult，pipeline 再落 JobMatch(ORM)
这样爬虫/匹配器单测无需建表，逻辑与存储解耦。
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .enums import JobBoard, MatchVerdict


class JobQuery(BaseModel):
    """一次岗位搜索的条件。keywords 通常取自 ResumeProfile.search_keywords。"""
    keywords: list[str] = Field(default_factory=list)
    city: str = ""
    salary_min: Optional[int] = None  # 期望月薪下限（元），用于平台侧筛选
    limit: int = 20


class JobCandidate(BaseModel):
    """爬虫 search_jobs 的产物——一个岗位的平台无关快照。"""
    board: JobBoard
    external_id: str           # 平台侧岗位 id；无则由爬虫用 url 的稳定 hash 兜底
    url: str = ""
    title: str = ""
    company: str = ""
    location: str = ""
    salary_text: str = ""      # 原始「25-40K·14薪」
    jd_text: str = ""
    tags: list[str] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)


class MatchResult(BaseModel):
    """matcher 对单个岗位的打分结论。"""
    score: float = 0.0                       # 0-100
    verdict: MatchVerdict = MatchVerdict.WEAK
    matched_points: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    reasoning: str = ""
    model: str = ""


class ApplyResult(BaseModel):
    """applier.apply 的结果（P2 真投递用），对齐 core.schemas.PublishResult。"""
    success: bool
    external_id: Optional[str] = None   # 平台侧投递/会话 id
    url: Optional[str] = None
    error: Optional[str] = None
    raw: dict = Field(default_factory=dict)


class HrReply(BaseModel):
    """poll_replies 的产物（P3 HR 回复追踪用）。"""
    job_external_id: str = ""
    hr_name: str = ""
    text: str = ""
    raw: dict = Field(default_factory=dict)
