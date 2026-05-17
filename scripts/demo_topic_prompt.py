"""Demo — 验证专题 prompt 层被正确拼接到 ContentGenerator 的 system prompt。

不真调 LLM：用一个 FakeDriver 把传给 driver.complete() 的 system / user 拦下来打印。

跑法：
    python scripts/demo_topic_prompt.py

预期：
    打印三段 system prompt（dws / 软考 / 篮球vlog），每段都能看到对应 topic 的
    术语库和风格说明；以及一段"不传 topic_slug 时"的对照，证明向后兼容。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# 让脚本能直接 `python scripts/demo_topic_prompt.py` 跑，不强依赖 PYTHONPATH。
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ai_ops.content.generator import ContentGenerator, LLMDriver  # noqa: E402


class FakeDriver(LLMDriver):
    """不调 LLM，把最近一次的 system / user 留在实例上供 demo 打印。"""

    def __init__(self) -> None:
        self.last_system: str = ""
        self.last_user: str = ""

    async def complete(self, system: str, user: str, **kwargs) -> str:
        self.last_system = system
        self.last_user = user
        # 返回一段假的 LLM 输出，generator 会把它截到 1000 字塞进 body
        return "（FakeDriver：不调真实 LLM，仅演示 prompt 拼接）"


PERSONA = {"nickname": "demo-account", "tone": "专业克制"}
KEYWORDS_BY_SLUG = {
    "dws": ["AI 表格", "智能审批", "200 人 SaaS"],
    "软考": ["软件设计师", "进程调度", "近年真题"],
    "篮球vlog": ["库追", "野球场", "KD15"],
}
PLATFORM_BY_SLUG = {
    "dws": "公众号",
    "软考": "知乎",
    "篮球vlog": "小红书",
}


def _print_banner(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def _excerpt(text: str, n: int = 800) -> str:
    """打印前 N 字，超长截断；用于 demo 输出可读。"""
    if len(text) <= n:
        return text
    return text[:n] + f"\n... [已截断，total={len(text)} 字]"


async def _run_one(slug: str) -> None:
    driver = FakeDriver()
    gen = ContentGenerator(driver=driver)
    await gen.generate(
        topic_name=f"demo-article-for-{slug}",
        keywords=KEYWORDS_BY_SLUG[slug],
        persona=PERSONA,
        platform_hint=PLATFORM_BY_SLUG[slug],
        topic_slug=slug,
    )
    _print_banner(f"topic_slug = {slug!r}    platform = {PLATFORM_BY_SLUG[slug]!r}")
    print(_excerpt(driver.last_system, 1200))
    print()
    print(f"[user prompt] {driver.last_user}")


async def _run_backward_compat() -> None:
    """不传 topic_slug — 必须和老行为一致：system prompt 里没有 [专题层] 段。"""
    driver = FakeDriver()
    gen = ContentGenerator(driver=driver)
    await gen.generate(
        topic_name="老调用方式 — 不传 topic_slug",
        keywords=["legacy", "compat"],
        persona=PERSONA,
        platform_hint="通用",
    )
    _print_banner("向后兼容 — 不传 topic_slug，应当无 [专题层] 段")
    print(driver.last_system)
    print()
    assert "专题层 prompt" not in driver.last_system, "BUG: 不传 topic_slug 不应注入专题层！"
    print("[OK] 不传 topic_slug 时 system prompt 无专题层段，向后兼容通过。")


async def main() -> None:
    for slug in ("dws", "软考", "篮球vlog"):
        await _run_one(slug)
    await _run_backward_compat()


if __name__ == "__main__":
    asyncio.run(main())
