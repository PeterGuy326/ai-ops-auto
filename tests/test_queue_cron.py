"""TaskQueue.schedule_cron Linux cron 语义单测（TD-A1 根治验证）。

历史教训（上轮 sprint）：
  schedule_cron("0 9 * * 1") 把 "1" 透传给 APScheduler day_of_week，
  但 APScheduler 是 mon=0..sun=6，Linux 是 sun=0..sat=6，
  → 周一漂到周二。

本测试覆盖：
  - 0 9 * * 1 → 周一（weekday()==0）
  - 0 9 * * 0 → 周日（weekday()==6）
  - 0 9 * * 5 → 周五（weekday()==4）
  - 7 也算 sun
  - 字面量 mon-fri 直通不报错
  - 区间 1-5 → mon-fri
  - 列表 1,3,5 → mon,wed,fri
  - * 透传
  - 越界数字报错

实现说明：测试不 start scheduler（避免污染 event loop），用
  trigger.get_next_fire_time(None, now) 拿首次触发时间断言。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai_ops.scheduler.queue import TaskQueue


def _next_fire(q: TaskQueue, job_id: str) -> datetime:
    job = q._scheduler.get_job(job_id)
    assert job is not None, f"job {job_id} 未注册成功"
    now = datetime.now(timezone.utc)
    nxt = job.trigger.get_next_fire_time(None, now)
    assert nxt is not None, f"job {job_id} 没有 next_fire_time"
    return nxt


@pytest.fixture()
def q():
    """每个 case 用独立 TaskQueue，scheduler 不 start，避免污染。"""
    queue = TaskQueue()
    yield queue
    # 注册时未 start 也要清理，避免 add_job 留下持久状态（AsyncIOScheduler
    # 默认 MemoryJobStore，实例销毁就回收，这里 shutdown 是兜底）
    try:
        if queue._scheduler.running:
            queue._scheduler.shutdown(wait=False)
    except Exception:
        pass


async def _noop():
    """coroutine factory placeholder——不会被实际调用（scheduler 没 start）。"""
    return None


# ---------- 核心三件套：周一 / 周日 / 周五 ----------

def test_schedule_cron_monday(q):
    """0 9 * * 1 → 每周一 09:00。"""
    jid = q.schedule_cron("0 9 * * 1", _noop, job_id="td_a1_mon")
    nxt = _next_fire(q, jid)
    assert nxt.weekday() == 0, f"周一应 weekday=0，实际 {nxt.weekday()} ({nxt})"
    assert nxt.hour == 9 and nxt.minute == 0


def test_schedule_cron_sunday(q):
    """0 9 * * 0 → 每周日 09:00（weekday()==6）。"""
    jid = q.schedule_cron("0 9 * * 0", _noop, job_id="td_a1_sun")
    nxt = _next_fire(q, jid)
    assert nxt.weekday() == 6, f"周日应 weekday=6，实际 {nxt.weekday()} ({nxt})"
    assert nxt.hour == 9 and nxt.minute == 0


def test_schedule_cron_friday(q):
    """0 9 * * 5 → 每周五 09:00（weekday()==4）。"""
    jid = q.schedule_cron("0 9 * * 5", _noop, job_id="td_a1_fri")
    nxt = _next_fire(q, jid)
    assert nxt.weekday() == 4, f"周五应 weekday=4，实际 {nxt.weekday()} ({nxt})"
    assert nxt.hour == 9 and nxt.minute == 0


# ---------- 边界：7 = sun（Linux 兼容） ----------

def test_schedule_cron_dow_7_is_sunday(q):
    """0 9 * * 7 → Linux cron 兼容 7=sun（与 0 同语义）。"""
    jid = q.schedule_cron("0 9 * * 7", _noop, job_id="td_a1_sun_7")
    nxt = _next_fire(q, jid)
    assert nxt.weekday() == 6, f"7 应算周日 weekday=6，实际 {nxt.weekday()}"


# ---------- 字面量直通（向后兼容已有使用方式） ----------

def test_schedule_cron_literal_mon_passthrough(q):
    """0 9 * * mon → 字面量直通，仍是周一。"""
    jid = q.schedule_cron("0 9 * * mon", _noop, job_id="td_a1_lit_mon")
    nxt = _next_fire(q, jid)
    assert nxt.weekday() == 0


def test_schedule_cron_literal_range_passthrough(q):
    """0 9 * * mon-fri → 字面量区间直通，下次触发落在工作日。"""
    jid = q.schedule_cron("0 9 * * mon-fri", _noop, job_id="td_a1_lit_workdays")
    nxt = _next_fire(q, jid)
    assert nxt.weekday() <= 4, f"工作日 weekday 应 ≤ 4，实际 {nxt.weekday()}"


# ---------- 区间 / 列表 / 通配 ----------

def test_schedule_cron_range_1_5_is_workdays(q):
    """0 9 * * 1-5 → Linux 周一到周五 → APScheduler mon-fri。"""
    jid = q.schedule_cron("0 9 * * 1-5", _noop, job_id="td_a1_range")
    nxt = _next_fire(q, jid)
    assert nxt.weekday() <= 4


def test_schedule_cron_list_1_3_5(q):
    """0 9 * * 1,3,5 → Linux 一/三/五 → APScheduler mon,wed,fri。"""
    jid = q.schedule_cron("0 9 * * 1,3,5", _noop, job_id="td_a1_list")
    nxt = _next_fire(q, jid)
    assert nxt.weekday() in (0, 2, 4), f"应在 mon/wed/fri，实际 {nxt.weekday()}"


def test_schedule_cron_star_passthrough(q):
    """0 2 * * * → 任意天，hour=2。验证不破坏 health.py 使用方式。"""
    jid = q.schedule_cron("0 2 * * *", _noop, job_id="td_a1_star")
    nxt = _next_fire(q, jid)
    assert nxt.hour == 2 and nxt.minute == 0


# ---------- 错误处理 ----------

def test_schedule_cron_invalid_parts():
    queue = TaskQueue()
    with pytest.raises(ValueError, match="非法 cron"):
        queue.schedule_cron("0 9 * *", _noop, job_id="bad")


def test_schedule_cron_dow_out_of_range():
    queue = TaskQueue()
    with pytest.raises(ValueError, match="越界"):
        queue.schedule_cron("0 9 * * 8", _noop, job_id="bad_dow")


# ---------- helper 内部 ----------

def test_translate_linux_dow_pure():
    """直接打 helper，确保翻译表正确。"""
    t = TaskQueue._translate_linux_dow
    assert t("*") == "*"
    assert t("0") == "sun"
    assert t("1") == "mon"
    assert t("5") == "fri"
    assert t("6") == "sat"
    assert t("7") == "sun"  # 兼容
    assert t("1-5") == "mon-fri"
    assert t("1,3,5") == "mon,wed,fri"
    assert t("*/2") == "*/2"
    assert t("1-5/2") == "mon-fri/2"
    assert t("mon") == "mon"  # 字面量直通
    assert t("mon-fri") == "mon-fri"
