#!/usr/bin/env python3
"""human_pace.py — 浏览器发布动作的统一人类节奏控制。

节奏控制：按动作语义分档的随机停顿（humanPace 系列）：
按**动作语义**（而不是散落的固定毫秒）生成停顿——任务切换、字段切换、验证、
复核、提交各有其自然的停顿区间，并用两个随机样本的均值（centered random）
削弱「总取到区间端点」的不自然感。

用法（脚本内）：
    import human_pace
    human_pace.pace("task-switch")                  # 动作间停顿
    human_pace.pace("review", content_length)       # 复核停顿（按内容长度加权）
    human_pace.pause_before_commit(content_length)  # 填完后：复核 + 提交双停顿

测试/联调时置 EASEL_PACE_SKIP=1 全局跳过等待（selftest 用它保持离线快跑）。
"""
from __future__ import annotations

import os
import random
import time

# 各动作语义的停顿区间（毫秒）——各档区间如下
HUMAN_PACE_RANGES: dict[str, tuple[int, int]] = {
    "task-switch": (1800, 4500),   # 从一个任务切到另一个（打开页/进入编辑器）
    "field-switch": (650, 1800),   # 字段之间切换（标题→简介）
    "verification": (1200, 3000),  # 验证相关动作之间
    "review": (2000, 5000),        # 提交前的复核（再加内容长度加权）
    "commit": (1200, 3200),        # 复核完成到点下提交的反应时间
}


def sample_ms(stage: str, content_length: int = 0, rng=random.random) -> int:
    """按阶段采样停顿毫秒数。两个随机样本取均值 → 减少取到区间端点的概率。
    review 阶段额外加 min(8s, 内容长度×12ms) —— 内容越多「看」得越久。"""
    lo, hi = HUMAN_PACE_RANGES[stage]
    centered = (rng() + rng()) / 2
    review_extra = min(8000, max(0, content_length) * 12) if stage == "review" else 0
    return round(lo + (hi - lo) * centered + review_extra)


def pace(stage: str, content_length: int = 0) -> None:
    """等待一个该阶段的自然停顿。EASEL_PACE_SKIP=1 时跳过（测试）。"""
    if os.environ.get("EASEL_PACE_SKIP") == "1":
        return
    time.sleep(sample_ms(stage, content_length) / 1000.0)


def pause_before_commit(content_length: int = 0) -> None:
    """填写完成后、点提交前：先复核（按内容长度）再以独立反应时间提交。
    （复核与提交是两个分离的停顿）"""
    pace("review", content_length)
    pace("commit")
