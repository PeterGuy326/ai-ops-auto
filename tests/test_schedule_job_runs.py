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
    """调度器可用时：先持久化 jitter 时间，再注册一次性 callback。"""
    calls = []

    def fake_schedule_once(when, factory, job_id=None):
        calls.append((when, job_id))
        return job_id

    from ai_ops.scheduler import worker as w
    monkeypatch.setattr(w, "settings", SimpleNamespace(auto_publish_enabled=True))
    monkeypatch.setattr(w, "execute_job", lambda jid: None)  # 不真执行
    from ai_ops.scheduler import jitter as jitter_mod
    monkeypatch.setattr(jitter_mod, "jitter_publish_time", lambda when: when)
    from ai_ops.scheduler.queue import queue
    monkeypatch.setattr(queue, "_scheduler", SimpleNamespace(running=True))
    monkeypatch.setattr(queue, "schedule_once", fake_schedule_once)

    base = datetime(2026, 6, 22, 10, 0, 0)
    jobs = [SimpleNamespace(id=10, scheduled_at=None), SimpleNamespace(id=11, scheduled_at=base)]
    out = schedule_job_runs(jobs, default_when=base)
    assert len(out) == 2
    assert {c[1] for c in calls} == {"pub-10", "pub-11"}  # 按 job id 排期
    assert calls[1][0] == base  # job 11 用自己的 scheduled_at
    assert all(job.scheduled_at == base for job in jobs)


def test_queue_stopped_still_persists_and_returns_jittered_time(monkeypatch):
    """API 进程不持有 queue 时，DB worker 仍按持久化 actual 恢复。"""
    from ai_ops.scheduler import jitter as jitter_mod
    from ai_ops.scheduler import worker as w
    from ai_ops.scheduler.queue import queue

    monkeypatch.setattr(w, "settings", SimpleNamespace(auto_publish_enabled=True))
    monkeypatch.setattr(queue, "_scheduler", SimpleNamespace(running=False))
    planned = datetime(2026, 6, 22, 10, 0, 0)
    actual = datetime(2026, 6, 22, 10, 7, 0)
    monkeypatch.setattr(jitter_mod, "jitter_publish_time", lambda when: actual)

    job = SimpleNamespace(id=12, scheduled_at=planned)
    assert schedule_job_runs([job]) == [(12, actual)]
    assert job.scheduled_at == actual


def test_auto_publish_disabled_leaves_jobs_pending(monkeypatch):
    """默认安全闸关闭时，不向内存调度器注册任何真发布回调。"""
    from ai_ops.scheduler import worker as w
    from ai_ops.scheduler.queue import queue

    calls = []
    monkeypatch.setattr(w, "settings", SimpleNamespace(auto_publish_enabled=False))
    monkeypatch.setattr(
        queue,
        "schedule_publish",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    jobs = [SimpleNamespace(id=20, scheduled_at=None)]
    assert schedule_job_runs(jobs) == []
    assert calls == []
