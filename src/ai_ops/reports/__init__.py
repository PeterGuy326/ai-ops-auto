"""数据回流自动出报 — L5 监测层 CLI 入口 + cron。

底层逻辑：SOP 文档已锁死日报/周报模板（docs/metrics-feedback-sop.md §四 / §五），
本包负责把模板从纸面落到可跑命令 + 可定时调度。

模块切分：
- daily.py：日报构建 + 写盘 + cron 入口
- weekly.py：周报构建 + 写盘 + cron 入口
- notifier_stub.py：DEPRECATED 占位，已切 ai_ops.notify.report_ready（下个清理 sprint 删）
- cron.py：APScheduler 注册（被 api/main.py lifespan 调用）
- cli_commands.py：typer 子组（被 cli.py 一行挂载）
"""

from .daily import build_daily_report, write_daily_report, run_daily_report_job
from .weekly import build_weekly_report, write_weekly_report, run_weekly_report_job
from ..notify import report_ready  # TD-A3：切真实 notify，stub 仅作兜底保留

__all__ = [
    "build_daily_report",
    "write_daily_report",
    "run_daily_report_job",
    "build_weekly_report",
    "write_weekly_report",
    "run_weekly_report_job",
    "report_ready",
]
