"""AI 内容生成器 — LLM 抽象层。

driver 可切：openai / anthropic / deepseek / dashscope。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..config import settings
from ..core.schemas import ArticleIn
from .humanize import HumanizeOptions, ai_smell_score, humanize_for_xhs

# 专题 prompt 根目录：<repo_root>/prompts/topics/
# generator.py 位于 src/ai_ops/content/generator.py，向上 4 层到 repo 根。
_TOPICS_PROMPT_DIR = Path(__file__).resolve().parents[3] / "prompts" / "topics"


def _load_topic_prompt(topic_slug: str) -> str:
    """读取 prompts/topics/{topic_slug}.md。

    文件不存在或 slug 为空时返回空串——不抛异常，保证向后兼容。
    """
    if not topic_slug:
        return ""
    path = _TOPICS_PROMPT_DIR / f"{topic_slug}.md"
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


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
        topic_slug: str = "",
    ) -> dict:
        """生成一篇文章草稿。

        Args:
            topic_name: 文章主题字符串（业务上的"这篇文章讲什么"，会进 user prompt）。
            keywords: 关键词列表，会进 user prompt。
            persona: 人设画像 dict，会拼到 system prompt。
            platform_hint: 平台名（小红书/知乎/头条号/公众号/通用），决定 platform_style 走向。
            topic_slug: 专题 slug（文件名 stem），用于加载 prompts/topics/{slug}.md。
                例：dws / 软考 / 篮球vlog。为空或文件不存在时退化为旧行为，不破坏老调用。

        system prompt 拼接顺序：base + topic_prompt + platform_style_hint。
        """
        base_system = (
            "你是一名资深内容创作者。按目标平台调性输出可直接发布的文案。\n"
            f"人设：{persona}"
        )
        topic_prompt = _load_topic_prompt(topic_slug)
        platform_style_hint = f"平台：{platform_hint}"

        # 拼接顺序：base → 专题层（讲什么/讲给谁） → 平台层（怎么排版）
        system_parts = [base_system]
        if topic_prompt:
            system_parts.append(
                f"\n--- 专题层 prompt（topic={topic_slug}）---\n{topic_prompt}"
            )
        system_parts.append(f"\n--- 平台层提示 ---\n{platform_style_hint}")
        system = "\n".join(system_parts)

        user = (
            f"主题：{topic_name}\n关键词：{', '.join(keywords)}\n"
            "请输出 JSON：{title, body, tags:[], cover_hint}"
        )
        raw = await self.driver.complete(system, user)
        body = raw[:1000]
        # 反 AI 检测：小红书走 humanize 后处理；其它平台按需扩展
        if (
            settings.xhs_humanize_enabled
            and platform_hint
            and "小红书" in platform_hint
        ):
            before = ai_smell_score(body)
            body = humanize_for_xhs(body, HumanizeOptions())
            after = ai_smell_score(body)
            return {
                "title": topic_name,
                "body": body,
                "tags": keywords,
                "cover_hint": "",
                "humanize": {"ai_smell_before": round(before, 3), "ai_smell_after": round(after, 3)},
            }
        return {"title": topic_name, "body": body, "tags": keywords, "cover_hint": ""}


def to_article_in(topic_id: int, gen_result: dict, content_type, target_platforms) -> ArticleIn:
    return ArticleIn(
        topic_id=topic_id,
        title=gen_result["title"],
        body=gen_result["body"],
        content_type=content_type,
        target_platforms=target_platforms,
        extra={"tags": gen_result.get("tags", []), "cover_hint": gen_result.get("cover_hint", "")},
    )
