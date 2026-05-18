"""日报构建 — 严格对齐 docs/metrics-feedback-sop.md §四模板。

6 段固定渲染（即使数字为 0 / 空表也必须出现段落标题，保证模板稳定）：
  1. 今日发布数
  2. 平台分布（小红书 / 头条号 / 公众号）
  3. 主题分布
  4. 24h 内表现 TOP 3
  5. 发布失败
  6. 登录态失效账号

数据口径：UTC 半开区间 [date 00:00, date+1d 00:00)，与 DB `datetime.utcnow()` 对齐。
"""
from __future__ import annotations

from collections import Counter
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import session_scope
from ..core.enums import AccountHealth, JobStatus, Platform
from ..core.models import Account, Article, Metrics, PublishJob, Topic

# 段落标题常量 — 测试用例据此校验段数
SECTION_TITLES = (
    "今日发布",
    "平台分布",
    "今日发布主题分布",
    "24h 内表现 TOP 3",
    "发布失败",
    "登录态失效账号",
)

# SOP §四明确列举的三个核心平台
PLATFORM_LABELS = {
    Platform.XIAOHONGSHU.value: "小红书",
    Platform.TOUTIAO.value: "头条号",
    Platform.WECHAT_MP.value: "公众号",
}


def _day_window(d: date_cls) -> tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day)
    return start, start + timedelta(days=1)


def _success_jobs_in_window(
    s: Session, start: datetime, end: datetime
) -> list[PublishJob]:
    return list(
        s.execute(
            select(PublishJob)
            .where(PublishJob.finished_at >= start)
            .where(PublishJob.finished_at < end)
            .where(PublishJob.status == JobStatus.SUCCESS)
        ).scalars().all()
    )


def _failed_jobs_in_window(
    s: Session, start: datetime, end: datetime
) -> list[PublishJob]:
    return list(
        s.execute(
            select(PublishJob)
            .where(PublishJob.finished_at >= start)
            .where(PublishJob.finished_at < end)
            .where(PublishJob.status.in_(
                [JobStatus.FAILED.value, JobStatus.DEAD.value]
            ))
        ).scalars().all()
    )


def _platform_distribution(jobs: Iterable[PublishJob]) -> dict[str, int]:
    c: Counter = Counter()
    for j in jobs:
        c[str(j.platform)] += 1
    return c


def _accounts_for_jobs(
    s: Session, jobs: Iterable[PublishJob]
) -> dict[int, Account]:
    ids = {j.account_id for j in jobs}
    if not ids:
        return {}
    rows = s.execute(select(Account).where(Account.id.in_(ids))).scalars().all()
    return {a.id: a for a in rows}


def _topic_distribution(
    s: Session, jobs: Iterable[PublishJob]
) -> list[tuple[str, int]]:
    """按主题名聚合发布数，返回 [(topic_name, count)]，按 count 倒序。"""
    article_ids = {j.article_id for j in jobs}
    if not article_ids:
        return []
    arts = (
        s.execute(select(Article).where(Article.id.in_(article_ids)))
        .scalars().all()
    )
    topic_ids = {a.topic_id for a in arts}
    topic_map = {
        t.id: t.name
        for t in s.execute(select(Topic).where(Topic.id.in_(topic_ids)))
        .scalars().all()
    }
    article_topic = {a.id: a.topic_id for a in arts}
    c: Counter = Counter()
    for j in jobs:
        tid = article_topic.get(j.article_id)
        if tid is None:
            continue
        c[topic_map.get(tid, f"<topic#{tid}>")] += 1
    return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))


def _top3_24h(
    s: Session, jobs: list[PublishJob]
) -> list[tuple[PublishJob, Article | None, Metrics | None]]:
    """24h 内 TOP3 — 按 metric 最新一条的 (views + likes*10) 排序。

    互动权重高于曝光：与 SOP 表头一致（曝光 + 互动 双指标）。
    """
    if not jobs:
        return []
    job_map = {j.id: j for j in jobs}
    metrics_rows = (
        s.execute(
            select(Metrics).where(Metrics.job_id.in_(list(job_map.keys())))
            .order_by(Metrics.collected_at.desc())
        ).scalars().all()
    )
    latest: dict[int, Metrics] = {}
    for m in metrics_rows:
        if m.job_id not in latest:
            latest[m.job_id] = m

    def _score(j: PublishJob) -> int:
        m = latest.get(j.id)
        if m is None:
            return 0
        return (m.views or 0) + (m.likes or 0) * 10

    ranked = sorted(jobs, key=_score, reverse=True)[:3]
    art_ids = {j.article_id for j in ranked}
    art_map = {
        a.id: a
        for a in s.execute(select(Article).where(Article.id.in_(art_ids)))
        .scalars().all()
    } if art_ids else {}
    return [(j, art_map.get(j.article_id), latest.get(j.id)) for j in ranked]


def _expired_accounts(s: Session) -> list[Account]:
    return list(
        s.execute(
            select(Account).where(Account.health.in_(
                [AccountHealth.EXPIRED.value, AccountHealth.BANNED.value]
            ))
        ).scalars().all()
    )


def build_daily_report(s: Session, d: date_cls) -> str:
    """构建日报 markdown 字符串。

    严格 6 段：即使数据为 0 / 空表，每段标题都渲染。
    """
    start, end = _day_window(d)
    succ_jobs = _success_jobs_in_window(s, start, end)
    fail_jobs = _failed_jobs_in_window(s, start, end)
    acct_map = _accounts_for_jobs(s, succ_jobs + fail_jobs)

    plat_dist = _platform_distribution(succ_jobs)
    topic_dist = _topic_distribution(s, succ_jobs)
    top3 = _top3_24h(s, succ_jobs)
    expired = _expired_accounts(s)

    lines: list[str] = []
    lines.append(f"# [运营日报] {d.isoformat()}")
    lines.append("")

    # 1) 今日发布
    lines.append(f"## 今日发布：{len(succ_jobs)} 条")
    lines.append("")

    # 2) 平台分布
    lines.append("## 平台分布")
    for plat_value, label in PLATFORM_LABELS.items():
        n = plat_dist.get(plat_value, 0)
        # 该平台涉及的账号 nickname 列表
        nicks = sorted({
            (acct_map[j.account_id].nickname if j.account_id in acct_map else f"acct#{j.account_id}")
            for j in succ_jobs if str(j.platform) == plat_value
        })
        acct_part = f"（{', '.join(nicks)}）" if nicks else ""
        lines.append(f"- {label}：{n}{acct_part}")
    # 其它平台兜底列出（避免遗漏，但不影响 6 段结构）
    other = {k: v for k, v in plat_dist.items() if k not in PLATFORM_LABELS}
    if other:
        lines.append("- 其他平台：" + ", ".join(f"{k}={v}" for k, v in sorted(other.items())))
    lines.append("")

    # 3) 主题分布
    lines.append("## 今日发布主题分布")
    if topic_dist:
        for name, n in topic_dist:
            lines.append(f"- {name}：{n}")
    else:
        lines.append("- （今日无发布）")
    lines.append("")

    # 4) 24h TOP3
    lines.append("## 24h 内表现 TOP 3")
    if top3:
        for i, (job, art, m) in enumerate(top3, 1):
            title = art.title if art else f"<article#{job.article_id}>"
            acct = acct_map.get(job.account_id)
            acct_str = acct.nickname if acct else f"acct#{job.account_id}"
            views = m.views if m else 0
            engagement = ((m.likes if m else 0)
                          + (m.comments if m else 0)
                          + (m.shares if m else 0))
            lines.append(
                f"{i}. 《{title}》- {acct_str} - {job.platform} - "
                f"{views} 展示 / {engagement} 互动"
            )
    else:
        lines.append("- （24h 内无可统计数据）")
    lines.append("")

    # 5) 发布失败
    lines.append(f"## 发布失败：{len(fail_jobs)}（需人工处理）")
    for j in fail_jobs:
        acct = acct_map.get(j.account_id)
        acct_str = acct.nickname if acct else f"acct#{j.account_id}"
        err = (j.error or "").splitlines()[0][:120] if j.error else "(无错误信息)"
        lines.append(f"- job#{j.id} {j.platform} {acct_str}：{err}")
    lines.append("")

    # 6) 登录态失效账号
    lines.append("## 登录态失效账号")
    if expired:
        for a in expired:
            lines.append(f"- {a.nickname}（{a.platform}, health={a.health}）")
    else:
        lines.append("- （无）")
    lines.append("")

    return "\n".join(lines)


def write_daily_report(d: date_cls, out_dir: Path | str = "./reports") -> Path:
    """构建并写入 ./reports/daily-YYYY-MM-DD.md，返回路径。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"daily-{d.isoformat()}.md"
    with session_scope() as s:
        md = build_daily_report(s, d)
    path.write_text(md, encoding="utf-8")
    return path


async def run_daily_report_job() -> dict:
    """cron 入口 — 默认今日（UTC date）。"""
    from ..notify import report_ready

    d = datetime.utcnow().date()
    try:
        path = write_daily_report(d)
        report_ready("daily", str(path.resolve()))
        return {"ok": True, "kind": "daily", "date": d.isoformat(), "path": str(path)}
    except Exception as e:
        return {"ok": False, "kind": "daily", "date": d.isoformat(), "error": str(e)}
