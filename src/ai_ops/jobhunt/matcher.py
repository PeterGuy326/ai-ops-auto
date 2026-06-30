"""岗位匹配引擎 —— 简历 × JD → 打分 + 命中点/差距/理由（LLM 驱动）。

复用 content.generator 的 LLM driver（可注入 fake driver 离线测）。
输出 MatchResult，pipeline 再据 score 过阈值并落 JobMatch(ORM)。
"""
from __future__ import annotations

import json

from .enums import MatchVerdict
from .resume_parser import _strip_json  # 复用 JSON 抠取
from .schemas import JobCandidate, MatchResult

# matcher 用的 LLM driver（与 resume_parser 同源）；测试注入 fake
from ..content.generator import LLMDriver, get_driver


def _verdict_from_score(score: float) -> MatchVerdict:
    if score >= 75:
        return MatchVerdict.STRONG
    if score >= 55:
        return MatchVerdict.MAYBE
    return MatchVerdict.WEAK


class JobMatcher:
    def __init__(self, driver: LLMDriver | None = None):
        self.driver = driver or get_driver()

    async def score(self, resume_structured: dict, job: JobCandidate) -> MatchResult:
        """给单个岗位打分。resume_structured = ResumeProfile.structured。"""
        system = (
            "你是一名资深招聘匹配引擎。基于候选人结构化简历和岗位 JD，"
            "评估匹配度，给后续「是否投递」决策提供依据。\n"
            "严格只输出 JSON，不要解释、不要 markdown 代码块。字段：\n"
            "{\n"
            '  "score": 0-100 的整数,            // 综合匹配度\n'
            '  "matched_points": ["命中点1", ...], // 简历与 JD 的硬匹配项\n'
            '  "gaps": ["差距1", ...],            // JD 要求但简历缺/弱的点\n'
            '  "reasoning": "一句话总体判断"\n'
            "}\n"
            "打分原则：技能/年限/方向硬匹配为主，薪资城市为辅；宁可严，不要给人虚高的希望。\n"
            "重要：即使 JD 缺失或信息不全，也必须仅凭已有字段（标题/公司/标签/方向）尽力打分，"
            "绝不能反问、绝不能要求补充信息——任何情况下都只输出上述 JSON。"
            "JD 缺失时适当降低置信度并在 reasoning 注明『JD 缺失，依标题/标签推断』。"
        )
        jd_line = f"JD：{job.jd_text}" if job.jd_text.strip() else "JD：（列表页未提供，请依标题/标签/方向推断）"
        user = (
            "【候选人简历(JSON)】\n"
            f"{json.dumps(resume_structured, ensure_ascii=False)}\n\n"
            "【岗位】\n"
            f"标题：{job.title}\n公司：{job.company}\n城市：{job.location}\n"
            f"薪资：{job.salary_text}\n标签：{', '.join(job.tags)}\n"
            f"{jd_line}"
        )

        raw = await self.driver.complete(system, user, max_tokens=1200, temperature=0.2)
        data = self._parse(raw)

        score = data["score"]
        return MatchResult(
            score=score,
            verdict=_verdict_from_score(score),
            matched_points=data["matched_points"],
            gaps=data["gaps"],
            reasoning=data["reasoning"],
            model=getattr(self.driver, "model_name", type(self.driver).__name__),
        )

    @staticmethod
    def _parse(raw: str) -> dict:
        try:
            d = json.loads(_strip_json(raw))
        except json.JSONDecodeError as e:
            raise ValueError(f"matcher LLM 返回非法 JSON：{e}；原文前 300 字：{raw[:300]}") from e
        if not isinstance(d, dict):
            raise ValueError("matcher LLM 返回 JSON 顶层不是对象")

        # 归一化：score 夹到 0-100，list 字段兜底
        try:
            score = float(d.get("score", 0))
        except (TypeError, ValueError):
            score = 0.0
        score = max(0.0, min(100.0, score))

        def _as_list(v):
            if v is None:
                return []
            return v if isinstance(v, list) else [str(v)]

        return {
            "score": score,
            "matched_points": _as_list(d.get("matched_points")),
            "gaps": _as_list(d.get("gaps")),
            "reasoning": str(d.get("reasoning") or ""),
        }
