"""typer 子组 — `python -m ai_ops.cli report daily/weekly`。"""
from __future__ import annotations

from datetime import date as date_cls, datetime
from pathlib import Path

import typer

from .daily import write_daily_report
from ..notify import report_ready
from .weekly import parse_iso_week, write_weekly_report

report_app = typer.Typer(help="数据回流自动出报（日报 / 周报）")


@report_app.command("daily")
def cmd_daily(
    date: str = typer.Option(
        None,
        "--date",
        help="日期 YYYY-MM-DD，默认今日（UTC）",
    ),
    out_dir: Path = typer.Option(
        Path("./reports"), "--out-dir", help="输出目录，默认 ./reports"
    ),
    notify: bool = typer.Option(
        True, "--notify/--no-notify",
        help="是否调 ai_ops.notify.report_ready（默认 True；走飞书 webhook）",
    ),
):
    """生成日报到 ./reports/daily-YYYY-MM-DD.md。"""
    if date:
        try:
            d = date_cls.fromisoformat(date)
        except ValueError:
            typer.echo(f"非法日期：{date}", err=True)
            raise typer.Exit(2)
    else:
        d = datetime.utcnow().date()

    path = write_daily_report(d, out_dir)
    typer.echo(f"OK: daily report → {path}")
    if notify:
        report_ready("daily", str(path.resolve()))


@report_app.command("weekly")
def cmd_weekly(
    week: str = typer.Option(
        None,
        "--week",
        help="ISO 周 YYYY-Www，如 2026-W20。默认本周（UTC）",
    ),
    out_dir: Path = typer.Option(
        Path("./reports"), "--out-dir", help="输出目录，默认 ./reports"
    ),
    notify: bool = typer.Option(True, "--notify/--no-notify"),
):
    """生成周报到 ./reports/weekly-YYYY-Www.md。"""
    if week:
        try:
            year, w = parse_iso_week(week)
        except ValueError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(2)
    else:
        iso = datetime.utcnow().date().isocalendar()
        year, w = iso[0], iso[1]

    path = write_weekly_report(year, w, out_dir)
    typer.echo(f"OK: weekly report → {path}")
    if notify:
        report_ready("weekly", str(path.resolve()))
