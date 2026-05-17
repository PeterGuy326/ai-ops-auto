"""AI 内容生成器 — LLM 抽象层。

driver 可切：openai / anthropic / deepseek / dashscope。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import settings
from ..core.schemas import ArticleIn


class LLMDriver(ABC):
    @abstractmethod
    async def complete(self, system: str, user: str, **kwargs) -> str: ...


class OpenAIDriver(LLMDriver):
    async def complete(self, system: str, user: str, **kwargs) -> str:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        resp = await client.chat.completions.create(
            model=kwargs.get("model", "gpt-4o-mini"),
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=kwargs.get("temperature", 0.7),
        )
        return resp.choices[0].message.content or ""


class AnthropicDriver(LLMDriver):
    async def complete(self, system: str, user: str, **kwargs) -> str:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        resp = await client.messages.create(
            model=kwargs.get("model", "claude-opus-4-7"),
            max_tokens=kwargs.get("max_tokens", 2000),
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return resp.content[0].text if resp.content else ""


def get_driver() -> LLMDriver:
    if settings.llm_default == "anthropic":
        return AnthropicDriver()
    return OpenAIDriver()


class ContentGenerator:
    """根据主题 + 人设画像生成文章草稿。"""

    def __init__(self, driver: LLMDriver | None = None):
        self.driver = driver or get_driver()

    async def generate(
        self,
        topic_name: str,
        keywords: list[str],
        persona: dict,
        platform_hint: str = "通用",
    ) -> dict:
        system = (
            "你是一名资深内容创作者。按目标平台调性输出可直接发布的文案。\n"
            f"平台：{platform_hint}\n人设：{persona}"
        )
        user = (
            f"主题：{topic_name}\n关键词：{', '.join(keywords)}\n"
            "请输出 JSON：{title, body, tags:[], cover_hint}"
        )
        raw = await self.driver.complete(system, user)
        # 实际中要 robust 解析，这里给出占位
        return {"title": topic_name, "body": raw[:1000], "tags": keywords, "cover_hint": ""}


def to_article_in(topic_id: int, gen_result: dict, content_type, target_platforms) -> ArticleIn:
    return ArticleIn(
        topic_id=topic_id,
        title=gen_result["title"],
        body=gen_result["body"],
        content_type=content_type,
        target_platforms=target_platforms,
        extra={"tags": gen_result.get("tags", []), "cover_hint": gen_result.get("cover_hint", "")},
    )
