"""通用解析工具层 — UI 数字缩写 / 文本格式归一化。

定位：`core/` 基础设施层，被 `publishers/` + `scheduler/` 双向调用 = 正向分层。

背景（TD-Z3-debt 闭环, 2026 Q2）：
  上 sprint TD-Z3 让 `scheduler/worker.py` 反向 import `publishers/toutiao.py`
  的 `_parse_count`（L5 调 L4 是架构倒置）。本模块把通用解析逻辑沉到 core，
  让 publisher 和 scheduler 都正向 import，解除反向依赖。

设计原则：
  - 纯函数 + 失败降级（任何无法解析的输入返回 0，不抛）—— 调用方零 try/except
  - 不依赖业务对象、不开 session、不读 settings —— 纯文本输入/数值输出
  - bool 显式排除（Python 里 bool 是 int 子类，不排除会被静默吃成 1/0）
"""
from __future__ import annotations

import re

__all__ = ["parse_count"]


# UI 把大数缩写为 "1.2万" / "3.5k" / "1.5亿"，正则一次性捕获数字 + 单位
_COUNT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*([万亿wWkK]?)")


def parse_count(text: str | int | None) -> int:
    """把 UI 显示的计数文本/数值解析成 int。

    支持格式：
      "1234"      -> 1234
      "1.2万"     -> 12000
      "3.5k"      -> 3500
      "3.5K"      -> 3500
      "1.2w"      -> 12000
      "2亿"       -> 200000000
      "  234 "    -> 234（容忍前后空格）
      "阅读 1234" -> 1234（文本邻接抽数字）
      234（int）  -> 234（int 直通，其他 publisher 后续可能直接返 int）
      None / "" / "abc" / "--" -> 0（统一降级，不抛）
      True / False（bool）     -> 0（**bool 不当 1/0 静默吃**，必须显式排除）

    设计原则：
      - 头条 UI 显示精度就是 1.2 万（12000），业务接受这个精度——
        抓取的指标本就是估算值，不追求 sub-thousand 精确度。
      - 任何无法解析的输入返回 0，调用方不需要 try/catch。
      - bool 排除：Python 里 ``isinstance(True, int) is True``，不显式排除
        会让 True/False 被当成 1/0 静默落库，污染指标数据。
    """
    # bool 必须先排除 —— Python 里 bool 是 int 子类，
    # 不排除会让 True/False 被静默吃成 1/0，污染下游指标
    if isinstance(text, bool):
        return 0
    # int 直通 —— 其他 publisher 后续可能直接返 int 而非字符串
    if isinstance(text, int):
        return text
    if text is None:
        return 0
    s = str(text).strip()
    if not s:
        return 0
    m = _COUNT_RE.search(s)
    if not m:
        return 0
    try:
        num = float(m.group(1))
    except (ValueError, TypeError):
        return 0
    unit = m.group(2).lower()
    if unit in ("万", "w"):
        num *= 10000
    elif unit == "k":
        num *= 1000
    elif unit == "亿":
        num *= 100000000
    return int(num)
