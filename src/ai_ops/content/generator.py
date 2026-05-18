"""AI 内容生成器 — LLM 抽象层。

driver 可切：openai / anthropic / deepseek / dashscope。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..config import settings
from ..core.exceptions import DuplicateContentError
from ..core.schemas import ArticleIn
from .humanize import HumanizeOptions, ai_smell_score, humanize_for_xhs

# 同账号近 N 天内 simhash 重生循环参数（与 worker.SIMHASH_* 同语义）。
# 重生最多 2 轮：首次 + 2 轮重生 = 3 次 LLM 调用上限，控住配额。
_DEDUP_MAX_REGEN = 2
_DEDUP_LOOKBACK_DAYS = 7
_DEDUP_HAMMING_THRESHOLD = 8

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
        *,
        account_id_hint: int | None = None,
        similarity_checker=None,
    ) -> dict:
        """生成一篇文章草稿。

        Args:
            topic_name: 文章主题字符串（业务上的"这篇文章讲什么"，会进 user prompt）。
            keywords: 关键词列表，会进 user prompt。
            persona: 人设画像 dict，会拼到 system prompt。
            platform_hint: 平台名（小红书/知乎/头条号/公众号/通用），决定 platform_style 走向。
            topic_slug: 专题 slug（文件名 stem），用于加载 prompts/topics/{slug}.md。
                例：dws / 软考 / 篮球vlog。为空或文件不存在时退化为旧行为，不破坏老调用。
            account_id_hint: 目标账号 id；若提供则启用 simhash 重生循环——生成完文案后
                查该账号近 7d 已 PUBLISHED 历史，hamming < 8 即重生，最多 2 轮；3 轮仍重复
                抛 ``DuplicateContentError``。未提供则跳过重生（向后兼容；worker 前置 hook 兜底）。
            similarity_checker: 可注入的相似度检测函数（签名同 is_too_similar），
                单测注入 mock 用；生产路径默认 = ``core.dedup.is_too_similar``。

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

        # 重生循环：最多 _DEDUP_MAX_REGEN 轮重生；首次 + 重生共 _DEDUP_MAX_REGEN+1 次调用
        # 仅在调用方传 account_id_hint 时启用，否则保持旧行为（zero-cost 向后兼容）
        attempts = _DEDUP_MAX_REGEN + 1 if account_id_hint is not None else 1
        checker = similarity_checker
        if account_id_hint is not None and checker is None:
            # 延迟 import，避免冷启动多拉一次 dedup 模块
            from ..core.dedup import is_too_similar as _is_too_similar
            checker = _is_too_similar

        last_body = ""
        for attempt in range(attempts):
            raw = await self.driver.complete(system, user)
            body = raw[:1000]
            last_body = body
            if account_id_hint is None:
                # 旧路径：不做查重，直出
                break
            # 新路径：每次生成后查 simhash；命中且还有重生次数则继续 loop
            try:
                too_similar = checker(
                    text=body,
                    account_id=account_id_hint,
                    days=_DEDUP_LOOKBACK_DAYS,
                    threshold=_DEDUP_HAMMING_THRESHOLD,
                )
            except Exception:
                # 查重报错不阻断生成，按"没重复"放行
                too_similar = False
            if not too_similar:
                break
            if attempt == attempts - 1:
                # 最后一轮仍重复 → 抛出，调用方自己换关键词 / 换账号
                raise DuplicateContentError(
                    f"生成 {attempts} 轮后仍与账号 {account_id_hint} 近 "
                    f"{_DEDUP_LOOKBACK_DAYS}d 已发布内容过于相似（hamming < "
                    f"{_DEDUP_HAMMING_THRESHOLD}）"
                )

        body = last_body
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
