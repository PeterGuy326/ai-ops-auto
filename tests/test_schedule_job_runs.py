"""分发→真发布接线单测：schedule_job_runs 把 PENDING 任务排期到调度器。

  1. 调度器启动时：每条 job 排期成功，返回 (job_id, 实际触发时间)
  2. 调度器未启动/无 loop：静默跳过，不抛错（保证分发建记录不受影响）
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from ai_ops.scheduler.worker import schedule_job_runs


def test_skips_silently_when_scheduler_not_running():
    """无运行中调度器（单测/CLI）→ 不抛错，返回空或部分。"""
    jobs = [SimpleNamespace(id=1, scheduled_at=None), SimpleNamespace(id=2, scheduled_at=None)]
    # 不应抛异常（容错）
    result = schedule_job_runs(jobs)
    assert isinstance(result, list)


def test_schedules_when_queue_started(monkeypatch):
    """调度器可用时：每条 job 调用 queue.schedule_publish。"""
    calls = []

    def fake_schedule_publish(when, factory, job_id=None):
        calls.append((when, job_id))
        return (job_id, when)

    from ai_ops.scheduler import worker as w
    monkeypatch.setattr(w, "execute_job", lambda jid: None)  # 不真执行
    from ai_ops.scheduler.queue import queue
    monkeypatch.setattr(queue, "schedule_publish", fake_schedule_publish)

    base = datetime(2026, 6, 22, 10, 0, 0)
    jobs = [SimpleNamespace(id=10, scheduled_at=None), SimpleNamespace(id=11, scheduled_at=base)]
    out = schedule_job_runs(jobs, default_when=base)
    assert len(out) == 2
    assert {c[1] for c in calls} == {"pub-10", "pub-11"}  # 按 job id 排期
    assert calls[1][0] == base  # job 11 用自己的 scheduled_at
