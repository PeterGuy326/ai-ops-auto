"""核心异常定义。

只放跨模块共享的异常类型，不放业务私有错误。
"""
from __future__ import annotations


class DuplicateContentError(Exception):
    """内容生成器重生 N 轮仍与账号历史发布内容过于相似，无法继续生成。

    通常由 content/generator.py 在 simhash 命中后抛出，上游应：
      1. 换关键词 / 改 persona 重试
      2. 或换账号
      3. 或人工介入
    """
