"""文本查重（simhash 骨架）。

本模块只暴露接口，下一个 sprint 接入发布流程。

依赖策略：
  - 优先使用第三方 `simhash` 库（pyproject dev extra 已声明）
  - ImportError 时回退到内置 64-bit hash 算法（轻量级实现，单测可过）
  - 这样测试在干净环境 / dev 环境都能跑

接口：
  - compute_simhash(text) -> int       64-bit
  - hamming_distance(a, b) -> int      位差
  - is_too_similar(text, account_id, days=7, threshold=3) -> bool
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

# 第三方包可选 import
try:
    from simhash import Simhash as _Simhash  # type: ignore[import-not-found]
    _HAS_SIMHASH = True
except ImportError:
    _Simhash = None  # type: ignore[assignment]
    _HAS_SIMHASH = False


_TOKEN_RE = re.compile(r"[一-龥]|[A-Za-z0-9]+")
_HASH_BITS = 64


def _tokenize(text: str) -> list[str]:
    """简易分词：中文单字 + 英文/数字单词。

    生产环境可换 jieba / 字 n-gram，但骨架够用。
    """
    return _TOKEN_RE.findall(text or "")


def _hash64(s: str) -> int:
    """稳定 64-bit 内置 hash（不用 Python hash，避免随机化）。"""
    return int.from_bytes(hashlib.md5(s.encode("utf-8")).digest()[:8], "big")


def _fallback_simhash(tokens: list[str]) -> int:
    """无 simhash 包时的内置实现。

    标准 simhash 流程：
      1. 每个 token 取 64-bit hash
      2. 64 维向量，每位：hash 该位是 1 则 +weight，是 0 则 -weight
      3. 最终 > 0 那位 = 1，<= 0 那位 = 0
    """
    if not tokens:
        return 0
    vec = [0] * _HASH_BITS
    for tok in tokens:
        h = _hash64(tok)
        for i in range(_HASH_BITS):
            bit = (h >> i) & 1
            vec[i] += 1 if bit == 1 else -1
    fingerprint = 0
    for i in range(_HASH_BITS):
        if vec[i] > 0:
            fingerprint |= 1 << i
    return fingerprint


def compute_simhash(text: str) -> int:
    """计算文本的 64-bit simhash。空文本返回 0。"""
    tokens = _tokenize(text)
    if not tokens:
        return 0
    if _HAS_SIMHASH:
        return int(_Simhash(tokens, f=_HASH_BITS).value)
    return _fallback_simhash(tokens)


def hamming_distance(a: int, b: int) -> int:
    """两 simhash 的汉明距离（不同位的数量）。"""
    return bin(a ^ b).count("1")


def is_too_similar(
    text: str,
    account_id: int,
    days: int = 7,
    threshold: int = 3,
) -> bool:
    """检查 text 与该账号过去 N 天发布过的 articles.body 是否过于相似。

    距离 < threshold 即判太像（默认 threshold=3，对应 ~95% 相似度上限）。

    实现说明：
      - 通过 PublishJob.account_id 反查 Article.body（同账号发过的）
      - 只看 SUCCESS 状态的 job，drafts/failed 不算"已发布"
      - 拉不到数据时返回 False（新号没历史，放行）
    """
    from ..core.db import session_scope
    from ..core.enums import JobStatus
    from ..core.models import Article, PublishJob

    new_hash = compute_simhash(text)
    if new_hash == 0:
        return False

    since = datetime.utcnow() - timedelta(days=days)
    with session_scope() as s:
        rows = (
            s.query(Article.body)
            .join(PublishJob, PublishJob.article_id == Article.id)
            .filter(
                PublishJob.account_id == account_id,
                PublishJob.status == JobStatus.SUCCESS,
                PublishJob.finished_at >= since,
            )
            .all()
        )
        for (body,) in rows:
            if not body:
                continue
            prev = compute_simhash(body)
            if hamming_distance(new_hash, prev) < threshold:
                return True
    return False
