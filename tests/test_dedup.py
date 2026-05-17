"""core/dedup.py 单测：simhash + hamming + 与 db 集成的相似度判定。"""
from __future__ import annotations

from ai_ops.core.dedup import compute_simhash, hamming_distance


def test_simhash_identical_text_distance_zero():
    a = "今天天气真好，我去公园拍了几张照片。"
    h1 = compute_simhash(a)
    h2 = compute_simhash(a)
    assert h1 == h2
    assert hamming_distance(h1, h2) == 0


def test_simhash_different_text_distance_above_threshold():
    a = "今天天气真好，我去公园拍了几张照片。配文'温柔的下午'。"
    b = "新版 iPhone 摄像头评测：在低光环境下表现非常出色，超广角更进一步。"
    h1 = compute_simhash(a)
    h2 = compute_simhash(b)
    dist = hamming_distance(h1, h2)
    # 跨主题 simhash 一般距离 >> 3
    assert dist > 3


def test_simhash_empty_text():
    assert compute_simhash("") == 0
    assert compute_simhash("   ") == 0  # 纯空白也 tokenize 不出来


def test_hamming_distance_basic():
    assert hamming_distance(0, 0) == 0
    assert hamming_distance(0, 0xFF) == 8
    assert hamming_distance(0xFFFFFFFFFFFFFFFF, 0) == 64
    assert hamming_distance(0b1010, 0b0101) == 4


def test_simhash_near_duplicate_distance_small():
    """改一两个词的近似文本，距离应小（< 一半总位数）。"""
    a = "今天去咖啡馆拍了照，光线温柔，氛围很棒。配文：周末小确幸。"
    b = "今天去咖啡店拍了照，光线柔和，氛围很棒。配文：周末小确幸。"
    h1 = compute_simhash(a)
    h2 = compute_simhash(b)
    dist = hamming_distance(h1, h2)
    # 近似文本距离应远小于"完全不同主题"的距离，且 < 32（一半位数）
    assert dist < 32
