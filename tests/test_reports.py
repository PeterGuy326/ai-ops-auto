"""Task A · 数据回流自动出报 单测。

覆盖：
  - 空 DB 日报 6 段全在
  - 空 DB 周报 9 段全在
  - 有数据日报：TOP3 / 平台分布 / 主题分布 正确
  - 有数据周报：总曝光 / 主题 ROI / 爆款 TOP3 正确
  - parse_iso_week 边界
  - notifier_stub.report_ready 调用不抛
  - cli_commands 通过 typer CliRunner 跑通
  - cron.schedule_report_crons 可被 mock 调度器接住（job_id 固定）

不依赖真 DB 文件 — 用 in-memory sqlite。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from ai_ops.core.enums import (
    AccountHealth,
    ArticleStatus,
    ContentType,
    JobStatus,
    Platform,
)
from ai_ops.core.models import Account, Article, Base, Metrics, PublishJob, Topic
from ai_ops.reports.daily import (
    SECTION_TITLES as DAILY_SECTIONS,
    build_daily_report,
)
from ai_ops.reports.notifier_stub import report_ready
from ai_ops.reports.weekly import (
    SECTION_TITLES as WEEKLY_SECTIONS,
    build_weekly_report,
    parse_iso_week,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = SessionLocal()
    try:
        yield s
        s.commit()
    finally:
        s.close()
        engine.dispose()


# ---------- 空 DB 模板完整性 ----------

def test_daily_empty_db_all_sections(session):
    d = date(2026, 5, 18)
    md = build_daily_report(session, d)
    # 6 段标题必须都在（即使数据为 0）
    for title in DAILY_SECTIONS:
        assert title in md, f"日报缺段：{title}\n---\n{md}"
    # 顶部日期
    assert "2026-05-18" in md
    # 空数据兜底
    assert "今日发布：0 条" in md
    assert "（无）" in md  # 失效账号


def test_weekly_empty_db_all_sections(session):
    md = build_weekly_report(session, 2026, 20)
    for title in WEEKLY_SECTIONS:
        assert title in md, f"周报缺段：{title}\n---\n{md}"
    # 顶部周号
    assert "2026-W20" in md
    # ISO 周 20 = 周一 2026-05-11
    assert "2026-05-11" in md


# ---------- 业务数据 ----------

def _seed(s):
    t_tech = Topic(name="DWS集成", category="tech",
                   keywords=["integration-llm", "feature_a"])
    t_exam = Topic(name="软考", category="exam",
                   keywords=["feature_b"])
    s.add_all([t_tech, t_exam])
    s.flush()

    a_xhs = Account(platform=Platform.XIAOHONGSHU.value, nickname="dws_xhs_01",
                    topic_id=t_tech.id, health=AccountHealth.HEALTHY.value)
    a_tt = Account(platform=Platform.TOUTIAO.value, nickname="dws_tt_01",
                   topic_id=t_tech.id, health=AccountHealth.HEALTHY.value)
    a_gzh = Account(platform=Platform.WECHAT_MP.value, nickname="dws_gzh_01",
                    topic_id=t_tech.id, health=AccountHealth.EXPIRED.value)
    s.add_all([a_xhs, a_tt, a_gzh])
    s.flush()

    art1 = Article(topic_id=t_tech.id, title="LLM 集成实战",
                   content_type=ContentType.IMAGE_TEXT.value,
                   status=ArticleStatus.PUBLISHED.value)
    art2 = Article(topic_id=t_exam.id, title="软考冲刺",
                   content_type=ContentType.LONG_ARTICLE.value,
                   status=ArticleStatus.PUBLISHED.value)
    s.add_all([art1, art2])
    s.flush()

    # 今天的 jobs：UTC 当天，靠 finished_at 落在 [today, today+1)
    today = datetime.utcnow().replace(hour=10, minute=0, second=0, microsecond=0)

    j1 = PublishJob(article_id=art1.id, account_id=a_xhs.id,
                    platform=Platform.XIAOHONGSHU.value,
                    status=JobStatus.SUCCESS.value, finished_at=today)
    j2 = PublishJob(article_id=art1.id, account_id=a_tt.id,
                    platform=Platform.TOUTIAO.value,
                    status=JobStatus.SUCCESS.value, finished_at=today)
    j3 = PublishJob(article_id=art2.id, account_id=a_gzh.id,
                    platform=Platform.WECHAT_MP.value,
                    status=JobStatus.SUCCESS.value, finished_at=today)
    # 一个失败 job
    j4 = PublishJob(article_id=art2.id, account_id=a_xhs.id,
                    platform=Platform.XIAOHONGSHU.value,
                    status=JobStatus.FAILED.value, finished_at=today,
                    error="风控触发：请稍后重试")
    s.add_all([j1, j2, j3, j4])
    s.flush()

    # metrics：j1 最高曝光，j2 中等，j3 最低
    s.add_all([
        Metrics(job_id=j1.id, views=10000, likes=300, comments=50, shares=20),
        Metrics(job_id=j2.id, views=3000, likes=80, comments=10, shares=5),
        Metrics(job_id=j3.id, views=500, likes=20, comments=2, shares=1),
    ])
    s.commit()
    return today.date()


def test_daily_with_data(session):
    d = _seed(session)
    md = build_daily_report(session, d)

    # 3 条成功
    assert "今日发布：3 条" in md
    # 平台分布行
    assert "小红书：1（dws_xhs_01）" in md
    assert "头条号：1（dws_tt_01）" in md
    assert "公众号：1（dws_gzh_01）" in md
    # 主题分布
    assert "DWS集成：2" in md
    assert "软考：1" in md
    # TOP3 第一名是 j1（10000 views + 300 likes*10 = 13000 score）
    assert "《LLM 集成实战》" in md
    assert "10000 展示" in md
    # 失败
    assert "发布失败：1" in md
    assert "风控触发" in md
    # 失效账号
    assert "dws_gzh_01" in md
    assert "expired" in md


def test_weekly_with_data(session):
    d = _seed(session)
    iso = d.isocalendar()
    md = build_weekly_report(session, iso[0], iso[1])

    # 总曝光 = 10000+3000+500 = 13500
    assert "总曝光：13500" in md
    # 总互动 = (300+50+20) + (80+10+5) + (20+2+1) = 370 + 95 + 23 = 488
    assert "总互动：488" in md
    # 主题 ROI 排行有 DWS集成
    assert "DWS集成" in md
    # 爆款 TOP3 至少包含两个标题
    assert "《LLM 集成实战》" in md
    # product_features 桶
    assert "integration-llm" in md
    assert "feature_a" in md
    # 下周计划兜底
    assert "下周计划" in md
    assert "重点 push 的 product_features" in md
    # prompt 归因明确标 out of scope
    assert "out of scope, follow-up" in md


# ---------- parse_iso_week ----------

def test_parse_iso_week_valid():
    assert parse_iso_week("2026-W20") == (2026, 20)
    assert parse_iso_week("2025-W01") == (2025, 1)
    assert parse_iso_week("  2024-W52  ") == (2024, 52)


def test_parse_iso_week_invalid():
    with pytest.raises(ValueError):
        parse_iso_week("2026W20")  # 缺 -
    with pytest.raises(ValueError):
        parse_iso_week("2026-W54")  # 越界
    with pytest.raises(ValueError):
        parse_iso_week("not-a-week")


# ---------- notifier_stub ----------

def test_report_ready_stub_no_raise(capsys):
    report_ready("daily", "/tmp/test.md")
    captured = capsys.readouterr()
    assert "report_ready" in captured.err
    assert "/tmp/test.md" in captured.err


# ---------- CLI 子组 ----------

def test_cli_report_app_has_commands():
    from ai_ops.reports.cli_commands import report_app
    runner = CliRunner()
    result = runner.invoke(report_app, ["--help"])
    assert result.exit_code == 0
    assert "daily" in result.stdout
    assert "weekly" in result.stdout


# ---------- cron 注册 ----------

def test_schedule_report_crons_smoke():
    """注册不抛、job_id 固定、weekly 真的落在周一。

    历史教训：早期实现走 queue.schedule_cron("0 9 * * 1")，但 APScheduler
    day_of_week 语义 mon=0，会把 "1" 解析成周二。改走原生 day_of_week="mon"
    后必须 assert 触发时间是周一。
    """
    from ai_ops.reports import cron as cron_mod
    from ai_ops.scheduler.queue import queue

    # 清空可能的残留 job（重启幂等也要测试隔离）
    for jid in ("report-daily", "report-weekly"):
        try:
            queue._scheduler.remove_job(jid)
        except Exception:
            pass

    # AsyncIOScheduler.add_job 不要求 scheduler running 即可注册
    did, wid = cron_mod.schedule_report_crons()
    assert did == "report-daily"
    assert wid == "report-weekly"

    djob = queue._scheduler.get_job("report-daily")
    wjob = queue._scheduler.get_job("report-weekly")
    assert djob is not None and wjob is not None

    # 触发时间断言：weekly 必须是周一（weekday()==0）
    # scheduler 未 start 时 job.next_run_time 可能不存在，直接用 trigger.get_next_fire_time
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)
    nxt = wjob.trigger.get_next_fire_time(None, now_utc)
    assert nxt is not None
    assert nxt.weekday() == 0, f"weekly job 应该在周一触发，实际 weekday={nxt.weekday()} ({nxt})"
    assert nxt.hour == 9 and nxt.minute == 0

    # daily 触发时间断言：小时=18
    dnxt = djob.trigger.get_next_fire_time(None, now_utc)
    assert dnxt is not None and dnxt.hour == 18 and dnxt.minute == 0

    # 清理
    for jid in ("report-daily", "report-weekly"):
        try:
            queue._scheduler.remove_job(jid)
        except Exception:
            pass


# ---------- TD-A3 收口：真函数切换验证 ----------

def test_report_ready_real_no_raise():
    """TD-A3：A 切到 ai_ops.notify.report_ready 后，在飞书 webhook URL 空时不抛。

    底层逻辑：notify.report_ready 已经 @_safe 兜底 + webhook 层吞网络异常；
    无 FEISHU_WEBHOOK_URL 时应静默 logger.debug 而不向上抛——这是闭环交付的
    红线，发布主流程绝不能因为通知模块自身配置缺失炸掉。
    """
    import os
    from ai_ops.notify import report_ready as real_report_ready

    # 强制清空 webhook URL 走"不发"分支
    old = os.environ.pop("FEISHU_WEBHOOK_URL", None)
    try:
        # daily / weekly 各打一次，确保 kind 分支都覆盖
        real_report_ready("daily", "/tmp/td_a3_daily_test.md")
        real_report_ready("weekly", "/tmp/td_a3_weekly_test.md")
    finally:
        if old is not None:
            os.environ["FEISHU_WEBHOOK_URL"] = old


def test_reports_init_report_ready_is_real():
    """TD-A3：reports/__init__.py re-export 的 report_ready 必须是真函数。

    防回归：避免有人把 import 切回 stub。
    """
    from ai_ops.reports import report_ready as reports_re_export
    from ai_ops.notify import report_ready as notify_real
    # 同一对象 —— re-export 必须是真函数
    assert reports_re_export is notify_real, (
        "reports.__init__ re-export 的 report_ready 不是 ai_ops.notify 的真函数，"
        "TD-A3 切换失败"
    )
