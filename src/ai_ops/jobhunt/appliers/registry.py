"""Applier 注册中心 —— 招聘平台到适配器的路由 + fallback（镜像 publishers/registry.py）。

同一平台可注册多个实现（如 Boss 主力 Playwright + 兜底 API），priority 越小越先尝试。
新加平台只需注册新 Applier，不动 pipeline。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable

from ..enums import JobBoard
from .base import ApplierBase


class ApplierRegistry:
    def __init__(self) -> None:
        self._slots: dict[JobBoard, list[tuple[int, Callable[[], ApplierBase]]]] = defaultdict(list)

    def register(
        self,
        board: JobBoard,
        factory: Callable[[], ApplierBase],
        priority: int = 100,
    ) -> None:
        self._slots[board].append((priority, factory))
        self._slots[board].sort(key=lambda t: t[0])

    def resolve(self, board: JobBoard) -> list[ApplierBase]:
        """返回该平台所有 Applier，按优先级排序。调用方依次尝试。"""
        return [factory() for _, factory in self._slots.get(board, [])]

    def first(self, board: JobBoard) -> ApplierBase:
        """取该平台最高优先级 Applier；未注册则抛错。"""
        appliers = self.resolve(board)
        if not appliers:
            raise ValueError(f"招聘平台 {board} 未注册任何 Applier")
        return appliers[0]

    def supported_boards(self) -> list[JobBoard]:
        return list(self._slots.keys())


def build_default_registry() -> ApplierRegistry:
    """默认装配。

    P1：Boss 真爬取需真登录态，未配则 pipeline 用 FakeApplier 跑通候选池逻辑。
    P2：智联/猎聘/51job（表单式）落地后在此注册。
    """
    from .boss import BossApplier

    reg = ApplierRegistry()
    reg.register(JobBoard.BOSS, BossApplier, priority=10)
    return reg


default_registry = build_default_registry()
