"""tests/test_generator_dedup.py — ContentGenerator.generate 重生循环单测。

策略：
  - mock LLMDriver 返回固定文本两轮 → 第三轮抛 DuplicateContentError
  - mock 一次成功路径 → 验证 account_id_hint=None 跳过重生（向后兼容）
  - mock 第二轮就过 → 验证早停不浪费 LLM 调用
"""
from __future__ import annotations

import asyncio

import pytest

from ai_ops.content.generator import ContentGenerator, LLMDriver
from ai_ops.core.exceptions import DuplicateContentError


class FixedDriver(LLMDriver):
    """每次 complete 都返回同一段文本，用来制造"重复"。"""

    def __init__(self, body: str = "复制粘贴的同款文案"):
        self.body = body
        self.call_count = 0

    async def complete(self, system: str, user: str, **kwargs) -> str:
        self.call_count += 1
        return '{"title":"t","body":"' + self.body + '","tags":[],"cover_hint":""}'


class SequenceDriver(LLMDriver):
    """按预设序列依次返回不同文本，用来制造"先重复后过"场景。"""

    def __init__(self, bodies: list[str]):
        self.bodies = bodies
        self.call_count = 0

    async def complete(self, system: str, user: str, **kwargs) -> str:
        idx = min(self.call_count, len(self.bodies) - 1)
        self.call_count += 1
        return self.bodies[idx]


def _always_similar(**_):
    return True


def _never_similar(**_):
    return False


def test_regen_loop_raises_after_3_attempts():
    """always similar checker → 跑满 3 轮（首次 + 2 轮重生）后抛 DuplicateContentError。"""
    driver = FixedDriver()
    gen = ContentGenerator(driver=driver)

    with pytest.raises(DuplicateContentError) as excinfo:
        asyncio.run(
            gen.generate(
                topic_name="主题 A",
                keywords=["k1", "k2"],
                persona={"name": "P"},
                platform_hint="通用",  # 走 default 路径，避开 humanize 干扰
                account_id_hint=42,
                similarity_checker=_always_similar,
            )
        )
    # 3 次 LLM 调用（首次 + 2 轮重生）
    assert driver.call_count == 3
    # error message 带 account_id
    assert "42" in str(excinfo.value)


def test_no_hint_skips_regen_loop():
    """account_id_hint=None → 只调一次 LLM，不查重，不抛异常（向后兼容）。"""
    driver = FixedDriver()
    gen = ContentGenerator(driver=driver)

    result = asyncio.run(
        gen.generate(
            topic_name="主题 A",
            keywords=["k1"],
            persona={"name": "P"},
            platform_hint="通用",
        )
    )
    assert driver.call_count == 1
    assert result["body"]  # 有内容


def test_first_gen_passes_early_stop():
    """首次生成就不重复 → 只调 1 次 LLM。"""
    driver = FixedDriver()
    gen = ContentGenerator(driver=driver)

    result = asyncio.run(
        gen.generate(
            topic_name="主题 B",
            keywords=["k"],
            persona={"name": "P"},
            platform_hint="通用",
            account_id_hint=7,
            similarity_checker=_never_similar,
        )
    )
    assert driver.call_count == 1
    assert "body" in result


def test_second_attempt_passes():
    """首次重复 → 重生 1 次后过；第 2 次就停，不跑满 3 轮。"""
    driver = SequenceDriver(
        bodies=[
            '{"title":"t","body":"dup body","tags":[],"cover_hint":""}',
            '{"title":"t","body":"fresh body","tags":[],"cover_hint":""}',
        ]
    )
    gen = ContentGenerator(driver=driver)

    # checker：第 1 次返回 True（重复），之后 False
    state = {"n": 0}

    def staged(**_):
        state["n"] += 1
        return state["n"] == 1

    result = asyncio.run(
        gen.generate(
            topic_name="主题 C",
            keywords=["k"],
            persona={"name": "P"},
            platform_hint="通用",
            account_id_hint=9,
            similarity_checker=staged,
        )
    )
    assert driver.call_count == 2
    assert "fresh body" in result["body"]


def test_checker_exception_treated_as_not_similar():
    """checker 抛异常 → 视为不重复，单次调用直接通过。"""
    driver = FixedDriver()
    gen = ContentGenerator(driver=driver)

    def explode(**_):
        raise RuntimeError("dedup 炸了")

    result = asyncio.run(
        gen.generate(
            topic_name="主题 D",
            keywords=["k"],
            persona={"name": "P"},
            platform_hint="通用",
            account_id_hint=1,
            similarity_checker=explode,
        )
    )
    # checker 异常视为"没重复"，第一次就过
    assert driver.call_count == 1
    assert "body" in result
