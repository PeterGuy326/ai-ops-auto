"""打招呼语生成 —— 一份通用简历 + 千岗千面的招呼（用户决策）。

针对单个岗位 + 匹配结论，用第一人称生成 ~100-150 字的 Boss 开场白：
开门见山点出与 JD 的硬匹配，简短有礼，引导 HR 回复。LLM driver 可注入离线测。
"""
from __future__ import annotations

from ..content.generator import LLMDriver, get_driver
from .schemas import JobCandidate, MatchResult

# Boss 打招呼语长度护栏（太长 HR 不看；太短没信息）
_MIN_LEN = 40
_MAX_LEN = 220


class GreetingGenerator:
    def __init__(self, driver: LLMDriver | None = None):
        self.driver = driver or get_driver()

    async def generate(
        self,
        resume_structured: dict,
        job: JobCandidate,
        match: MatchResult | None = None,
    ) -> str:
        matched = "、".join(match.matched_points[:3]) if match and match.matched_points else ""
        system = (
            "你是候选人本人，正在 Boss 直聘上跟 HR 打招呼。"
            "写一段第一人称、口语、真诚、不卑不亢的开场白：\n"
            "1. 开门见山点出我与这个岗位最硬的 1-2 个匹配点。\n"
            "2. 100-150 字，别套话、别堆形容词、别用『贵公司』官腔。\n"
            "3. 结尾自然引导对方回复（如方便了解更多/可详聊）。\n"
            "只输出招呼语正文，不要任何前后缀、不要引号。"
        )
        user = (
            f"【我的简历要点】\n"
            f"定位：{resume_structured.get('summary', '')}\n"
            f"技能：{', '.join(resume_structured.get('skills', [])[:8])}\n"
            f"年限：{resume_structured.get('years_of_experience', '')}\n"
            f"【目标岗位】{job.title} @ {job.company}（{job.location}）\n"
            f"JD：{job.jd_text}\n"
            + (f"【已识别的匹配点】{matched}\n" if matched else "")
        )
        text = (await self.driver.complete(system, user, max_tokens=400, temperature=0.7)).strip()
        # 去掉模型偶尔加的包裹引号
        text = text.strip('「」""\'')
        return text[:_MAX_LEN]
