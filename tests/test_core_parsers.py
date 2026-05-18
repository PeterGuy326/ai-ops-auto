"""tests/test_core_parsers.py — TD-Z3-debt 闭环 · core/parsers.parse_count 契约验证。

战场：`src/ai_ops/core/parsers.py:parse_count`

背景：上 sprint TD-Z3 让 worker.py 反向 import publishers/toutiao.py 的 _parse_count（架构倒置）。
本 sprint 把通用 UI 数字解析逻辑沉到 core 层，让 publisher 和 scheduler 都正向 import。
本测试是新模块的契约守护 —— `tests/test_toutiao_publisher.py` 的 test_parse_count_*
仍走老路径（toutiao._parse_count 别名），是别名兼容性的天然守护测试，不动。

测试覆盖（8 用例）：
  - 中文单位：万 / 亿
  - 西文单位：k / K / w / W
  - 纯数字字符串
  - int 直通（新增能力，老 toutiao 路径只接受 str）
  - None / 空串
  - bool 排除（新增显式契约 —— 防止 True/False 被当 1/0 静默吃）
  - 垃圾输入降级 0
  - 文本邻接（"阅读 1234" → 1234，UI 标签场景）
"""
from __future__ import annotations

import pytest

from ai_ops.core.parsers import parse_count


# ============== 中文单位 ==============


def test_parse_count_supports_chinese_wan():
    """中文「万」单位：1.2万 → 12000，2万 → 20000。"""
    assert parse_count("1.2万") == 12000
    assert parse_count("2万") == 20000
    assert parse_count("0.5万") == 5000


def test_parse_count_supports_chinese_yi():
    """中文「亿」单位：1.5亿 → 150_000_000。"""
    assert parse_count("1.5亿") == 150_000_000
    assert parse_count("2亿") == 200_000_000


# ============== 西文单位 ==============


def test_parse_count_supports_k_unit():
    """k/K/w/W 单位：3.5k → 3500，10K → 10000，1.2w → 12000，5W → 50000。"""
    assert parse_count("3.5k") == 3500
    assert parse_count("10K") == 10000
    assert parse_count("1.2w") == 12000
    assert parse_count("5W") == 50000


# ============== 纯数字 / 空白 ==============


def test_parse_count_pure_int_str():
    """纯数字字符串 + 前后空格容忍。"""
    assert parse_count("234") == 234
    assert parse_count("  1000 ") == 1000
    assert parse_count("0") == 0


# ============== 新增能力：int 直通 ==============


def test_parse_count_int_input_passthrough():
    """int 直接输入应直通返回 —— 其他 publisher 后续可能直接返 int（zhihu JSON API）。

    老 toutiao 路径只接受 str，搬到 core 后扩展 int 直通，让所有 publisher 都能用。"""
    assert parse_count(234) == 234
    assert parse_count(0) == 0
    assert parse_count(1_000_000) == 1_000_000
    assert parse_count(-5) == -5  # 负数直通，调用方语义决定要不要 clamp


# ============== None / 空 ==============


def test_parse_count_none_returns_zero():
    """None / 空串 / 全空白 → 0（不抛，让调用方零 try/except）。"""
    assert parse_count(None) == 0
    assert parse_count("") == 0
    assert parse_count("   ") == 0


# ============== bool 排除（新增显式契约）==============


def test_parse_count_bool_excluded():
    """bool 不被当 1/0 静默吃 —— Python 里 isinstance(True, int) is True，
    必须显式排除，否则会污染下游指标（True → 1 个 view 这种荒谬数据）。"""
    assert parse_count(True) == 0
    assert parse_count(False) == 0


# ============== 垃圾输入降级 ==============


def test_parse_count_garbage_returns_zero():
    """无法解析的输入统一降级 0：'abc' / '--' / 纯符号。"""
    assert parse_count("abc") == 0
    assert parse_count("--") == 0
    assert parse_count("???") == 0


# ============== 文本邻接（UI 标签场景）==============


def test_parse_count_extracts_number_from_label_text():
    """UI 邻接抽数字场景：'阅读 1234' / '评论 5' —— 头条 _EXTRACT_CARD_JS 兜底路径会返回这种文本。"""
    assert parse_count("阅读 1234") == 1234
    assert parse_count("评论 5") == 5
    assert parse_count("点赞 3.5k") == 3500


# ============== 公共 API 暴露契约 ==============


def test_parse_count_is_in_public_api():
    """parse_count 必须在 __all__ 里 —— 这是 publisher / scheduler 双向 import 的公共契约。"""
    from ai_ops.core import parsers
    assert "parse_count" in parsers.__all__
