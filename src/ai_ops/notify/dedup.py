"""通知去重器 — 进程内滑窗去重。

底层逻辑：发布失败/账号失效/污点等事件在短时间内可能连环触发（比如 cookie 一过期，
同账号 5 个 PublishJob 同一分钟全炸），全推给运营群会刷屏 → 信号被噪音淹没 →
通知矩阵就形同虚设。所以同事件 + 同目标在滑窗内只让首条 + 第 N 次聚合放行。

实现选择：进程内字典 + threading.Lock。不依赖 Redis（本 sprint 单实例足够；
多实例部署 follow-up 时再切外部存储）。

策略：window 内同 key 的发送序号
  - 1: 放行（首条）
  - 2: 静默丢
  - threshold (默认 3): 放行（携带"5 分钟内第 N 次"聚合提示）
  - >threshold: 静默丢
即在 5 分钟窗口内最多发出 2 条（首条 + 阈值聚合），对齐 publishing-sop §八的反刷屏约束。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Optional

from ..config import settings


class Deduper:
    """滑窗去重器。

    每个实例独立维护时间戳字典，便于测试隔离（生产用 module 级单例 _default）。
    """

    def __init__(self, window_seconds: Optional[int] = None, threshold: Optional[int] = None):
        # 允许显式覆盖 settings 值（主要给单测用）；生产路径走默认 None → settings
        self._window = window_seconds if window_seconds is not None else settings.notify_dedup_window_seconds
        self._threshold = threshold if threshold is not None else settings.notify_dedup_threshold
        self._events: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def should_send(self, event_type: str, target_id: str) -> tuple[bool, Optional[str]]:
        """返回 (是否发送, 可选聚合提示)。

        Args:
            event_type: 事件类型（如 "publish_success" / "publish_failed" / "account_expired"）
            target_id: 事件目标 ID（如 job_id / account_id），用于在事件类型内进一步分桶

        Returns:
            (send, hint)：
              - send=True 表示应该真实发送
              - hint=None 表示首条；hint="5 分钟内第 N 次同类事件" 表示聚合放行
        """
        key = f"{event_type}:{target_id}"
        now = time.monotonic()
        cutoff = now - self._window

        with self._lock:
            timestamps = [t for t in self._events[key] if t > cutoff]
            timestamps.append(now)
            self._events[key] = timestamps
            count = len(timestamps)

            if count == 1:
                return True, None
            if count == self._threshold:
                return True, f"{self._window // 60} 分钟内第 {count} 次同类事件（已聚合）"
            # count == 2 或 count > threshold → 静默丢
            return False, None

    def reset(self) -> None:
        """清空状态。测试用。"""
        with self._lock:
            self._events.clear()


# Module 级单例，生产路径使用
_default = Deduper()


def should_send(event_type: str, target_id: str) -> tuple[bool, Optional[str]]:
    """便捷入口，等价于 _default.should_send。"""
    return _default.should_send(event_type, target_id)


def reset_for_test() -> None:
    """重置默认 deduper，给单测/E2E 验收脚本调用——避免跨用例污染。"""
    _default.reset()
