"""任务调度后端。第一版 APScheduler，可平滑切 Celery。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import settings
from ..observability import get_logger
from ..observability.sentry import capture_exception
from .jitter import jitter_publish_time

logger = get_logger(__name__)


class TaskQueue:
    """对调度后端的薄壳，业务代码不直接依赖 APScheduler。"""

    def __init__(self) -> None:
        # Cron expressions are operational local time; pinning the zone avoids
        # host/container defaults silently shifting daily reports and checks.
        scheduler_timezone = getattr(settings, "scheduler_timezone", "Asia/Shanghai")
        self._scheduler = AsyncIOScheduler(timezone=ZoneInfo(scheduler_timezone))

    def start(self) -> None:
        if not self._scheduler.running:
            self._scheduler.start()

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    @staticmethod
    async def _run_coro_factory(coro_factory: Callable[[], Awaitable[object]]) -> None:
        """Run callbacks as a native coroutine on AsyncIOScheduler's event loop.

        A synchronous lambda that calls ``asyncio.create_task`` is dispatched to
        APScheduler's thread pool, where no running event loop exists. Registering
        this async function makes AsyncIOExecutor await it in the scheduler loop.
        """
        await coro_factory()

    def schedule_once(
        self,
        when: datetime,
        coro_factory: Callable[[], Awaitable[object]],
        job_id: str | None = None,
    ) -> str:
        # Project datetimes are persisted as naive UTC. APScheduler otherwise
        # interprets a naive date in the scheduler's local timezone, which can
        # turn a future metrics callback into an immediate misfire.
        run_date = when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)
        job = self._scheduler.add_job(
            self._run_coro_factory,
            args=(coro_factory,),
            trigger="date",
            run_date=run_date,
            id=job_id,
            replace_existing=True,
        )
        return job.id

    # Linux cron 周几数字 → APScheduler 字面量
    # Linux cron：sun=0..sat=6（且 7 也算 sun）
    # APScheduler：mon=0..sun=6 —— 数字含义不一致，必须翻译
    _LINUX_DOW = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")

    @classmethod
    def _translate_linux_dow(cls, expr: str) -> str:
        """把 Linux cron 的 dow 段翻译成 APScheduler 字面量。

        支持：纯数字（0..7）、区间（1-5）、列表（1,3,5）、step（*/2 / 1-5/2）、
        * 通配、字面量（mon-fri / mon,wed）。

        策略：
        - * 直通
        - 含字母 → 视作字面量直通（APScheduler 原生支持）
        - 纯数字相关 → 按 _LINUX_DOW 映射每个数字（含 7→sun）
        - 混合用 ',' / '-' / '/' 拆分，递归处理
        """
        expr = expr.strip()
        if not expr or expr == "*":
            return expr
        # 含字母 = 用户已经用字面量，直通
        if any(c.isalpha() for c in expr):
            return expr
        # step：left/right —— 只翻译 left（right 是步长不动）
        if "/" in expr:
            left, _, step = expr.partition("/")
            return f"{cls._translate_linux_dow(left)}/{step}"
        # 列表
        if "," in expr:
            return ",".join(cls._translate_linux_dow(p) for p in expr.split(","))
        # 区间
        if "-" in expr:
            a, _, b = expr.partition("-")
            return f"{cls._translate_linux_dow(a)}-{cls._translate_linux_dow(b)}"
        # 纯数字
        if expr.isdigit():
            n = int(expr)
            if not 0 <= n <= 7:
                raise ValueError(f"cron dow 越界：{n}（合法 0..7，7=sun）")
            return cls._LINUX_DOW[n % 7]  # 7→sun
        # 其他（如 ?）直通让 APScheduler 自己报错
        return expr

    def schedule_cron(
        self,
        cron: str,
        coro_factory: Callable[[], Awaitable[object]],
        job_id: str | None = None,
    ) -> str:
        """注册 Linux cron 表达式。

        cron 字符串 5 段："分 时 日 月 周"，例如 "0 9 * * 1" → 每周一 09:00。
        dow 段按 **Linux cron 语义**解析（sun=0..sat=6，7 也=sun），
        内部翻译成 APScheduler 字面量后再透传——业务侧不用关心 APScheduler
        与 Linux cron 的数字错位。
        """
        parts = cron.split()
        if len(parts) != 5:
            raise ValueError(f"非法 cron：{cron}")
        minute, hour, day, month, dow = parts
        kwargs = {
            "minute": minute,
            "hour": hour,
            "day": day,
            "month": month,
            "day_of_week": self._translate_linux_dow(dow),
        }
        job = self._scheduler.add_job(
            self._run_coro_factory,
            args=(coro_factory,),
            trigger="cron",
            id=job_id,
            replace_existing=True,
            **kwargs,
        )
        return job.id

    def cancel(self, job_id: str) -> None:
        try:
            self._scheduler.remove_job(job_id)
        except Exception as e:
            # cancel 失败常见于 job 已被消费/不存在——业务路径上是幂等吞掉，但若是
            # APScheduler 内部状态损坏（如调度器关停中调 cancel）就会无声泄漏 job
            logger.warning(
                "scheduler.queue.cancel: swallowed",
                extra={"job_id": job_id, "error": str(e)},
            )
            capture_exception(e, scope="scheduler.queue.cancel", job_id=job_id)

    def schedule_publish(
        self,
        planned: datetime,
        coro_factory: Callable[[], Awaitable[object]],
        job_id: str | None = None,
        *,
        avoid_night: bool = True,
    ) -> tuple[str, datetime]:
        """带 jitter + 凌晨保护的发布调度。返回 (调度ID, 实际触发时间)。

        与 schedule_once 区别：本方法专为 PublishJob 设计，规避风控对"机器规律"的检测。
        """
        actual = jitter_publish_time(planned, avoid_night=avoid_night)
        sid = self.schedule_once(actual, coro_factory, job_id=job_id)
        return sid, actual


queue = TaskQueue()
