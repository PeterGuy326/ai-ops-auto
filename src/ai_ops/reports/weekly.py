"""周报构建 — 严格对齐 docs/metrics-feedback-sop.md §五模板。

9 段固定渲染：
  1. 本周发文 × 账号矩阵
  2. 总曝光
  3. 总互动
  4. 主题 ROI 排行
  5. 爆款 TOP 3（按互动率排序）
  6. 账号矩阵表现（最高/最低 ROI）
  7. product_features 热度（用 Topic.keywords 桶）
  8. prompt 归因（MVP: out of scope, follow-up — L3 prompt 元数据尚未落库）
  9. 下周计划（基于排行自动生成的建议）

ISO 周：`%G-W%V`，2026-W20 → fromisocalendar(2026, 20, 1) → Mon 2026-05-11。
窗口：UTC [Mon 00:00, next Mon 00:00)。
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import session_scope
from ..core.enums import JobStatus
from ..core.models import Account, Article, Metrics, PublishJob, Topic

SECTION_TITLES = (
    "本周发文",
    "总曝光",
    "总互动",
    "主题 ROI 排行",
    "爆款 TOP 3",
    "账号矩阵表现",
    "product_features 热度",
    "prompt 归因",
    "下周计划",
)

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{1,2})$")


def parse_iso_week(token: str) -> tuple[int, int]:
    """解析 '2026-W20' → (2026, 20)。"""
    m = _WEEK_RE.match(token.strip())
    if not m:
        raise ValueError(f"非法 ISO 周格式：{token}（期望 YYYY-Www，如 2026-W20）")
    year, week = int(m.group(1)), int(m.group(2))
    if not (1 <= week <= 53):
        raise ValueError(f"周数越界：{week}")
    return year, week


def _week_window(year: int, week: int) -> tuple[datetime, datetime]:
    monday = date_cls.fromisocalendar(year, week, 1)
    start = datetime(monday.year, monday.month, monday.day)
    return start, start + timedelta(days=7)


def _success_jobs(s: Session, start: datetime, end: datetime) -> list[PublishJob]:
    return list(
        s.execute(
            select(PublishJob)
            .where(PublishJob.finished_at >= start)
            .where(PublishJob.finished_at < end)
            .where(PublishJob.status == JobStatus.SUCCESS)
        ).scalars().all()
    )


def _latest_metric_per_job(
    s: Session, job_ids: list[int]
) -> dict[int, Metrics]:
    if not job_ids:
        return {}
    rows = (
        s.execute(
            select(Metrics).where(Metrics.job_id.in_(job_ids))
            .order_by(Metrics.collected_at.desc())
        ).scalars().all()
    )
    latest: dict[int, Metrics] = {}
    for m in rows:
        if m.job_id not in latest:
            latest[m.job_id] = m
    return latest


def build_weekly_report(s: Session, year: int, week: int) -> str:
    start, end = _week_window(year, week)
    jobs = _success_jobs(s, start, end)
    job_ids = [j.id for j in jobs]
    latest = _latest_metric_per_job(s, job_ids)

    # 关联 articles / topics / accounts
    art_ids = {j.article_id for j in jobs}
    arts = (
        s.execute(select(Article).where(Article.id.in_(art_ids))).scalars().all()
        if art_ids else []
    )
    art_map = {a.id: a for a in arts}
    topic_ids = {a.topic_id for a in arts}
    topics = (
        s.execute(select(Topic).where(Topic.id.in_(topic_ids))).scalars().all()
        if topic_ids else []
    )
    topic_map = {t.id: t for t in topics}
    acct_ids = {j.account_id for j in jobs}
    accts = (
        s.execute(select(Account).where(Account.id.in_(acct_ids))).scalars().all()
        if acct_ids else []
    )
    acct_map = {a.id: a for a in accts}

    # 聚合
    total_views = sum((latest[j.id].views if j.id in latest else 0) for j in jobs)
    total_engagement = sum(
        (latest[j.id].likes + latest[j.id].comments + latest[j.id].shares
         if j.id in latest else 0) for j in jobs
    )
    n_articles = len(art_ids)
    n_accounts = len(acct_ids)

    # 主题 ROI（CPM = views/1000 倒数概念这里简化为 平均 views 与 互动率）
    # 互动率 = engagement / max(views, 1)
    topic_stats: dict[int, dict] = defaultdict(
        lambda: {"posts": 0, "views": 0, "eng": 0}
    )
    for j in jobs:
        art = art_map.get(j.article_id)
        if not art:
            continue
        tid = art.topic_id
        m = latest.get(j.id)
        topic_stats[tid]["posts"] += 1
        if m:
            topic_stats[tid]["views"] += m.views or 0
            topic_stats[tid]["eng"] += (m.likes or 0) + (m.comments or 0) + (m.shares or 0)

    def _cpm(views: int, posts: int) -> float:
        # CPM 这里简化为"每千次曝光的发布投入"——posts 当成 1k 单位即可（数字稳）
        return round(posts * 1000.0 / max(views, 1), 2)

    def _rate(eng: int, views: int) -> float:
        return round(eng * 100.0 / max(views, 1), 2)

    topic_roi = []
    for tid, st in topic_stats.items():
        topic_roi.append({
            "name": topic_map[tid].name if tid in topic_map else f"<topic#{tid}>",
            "posts": st["posts"],
            "views": st["views"],
            "eng": st["eng"],
            "cpm": _cpm(st["views"], st["posts"]),
            "rate": _rate(st["eng"], st["views"]),
        })
    topic_roi.sort(key=lambda r: (-r["rate"], -r["views"]))

    # 爆款 TOP3（按互动率）
    def _job_rate(j: PublishJob) -> float:
        m = latest.get(j.id)
        if not m:
            return 0.0
        eng = (m.likes or 0) + (m.comments or 0) + (m.shares or 0)
        return eng / max(m.views or 0, 1)

    top3 = sorted(jobs, key=_job_rate, reverse=True)[:3]

    # 账号矩阵表现（按 互动率 排）
    acct_stats: dict[int, dict] = defaultdict(
        lambda: {"posts": 0, "views": 0, "eng": 0}
    )
    for j in jobs:
        m = latest.get(j.id)
        acct_stats[j.account_id]["posts"] += 1
        if m:
            acct_stats[j.account_id]["views"] += m.views or 0
            acct_stats[j.account_id]["eng"] += (m.likes or 0) + (m.comments or 0) + (m.shares or 0)
    acct_ranked = sorted(
        acct_stats.items(),
        key=lambda kv: (kv[1]["eng"] / max(kv[1]["views"], 1)),
        reverse=True,
    )

    # product_features 热度：用 Topic.keywords 桶（SOP 未明指字段，keywords 是最稳的桶）
    feat_counter_posts: Counter = Counter()
    feat_counter_views: Counter = Counter()
    for j in jobs:
        art = art_map.get(j.article_id)
        if not art:
            continue
        topic = topic_map.get(art.topic_id)
        if not topic or not topic.keywords:
            continue
        m = latest.get(j.id)
        v = m.views if m else 0
        for kw in topic.keywords:
            feat_counter_posts[kw] += 1
            feat_counter_views[kw] += v
    feat_rank = sorted(
        feat_counter_posts.items(),
        key=lambda kv: (-feat_counter_views[kv[0]], -kv[1]),
    )[:10]

    # 渲染
    lines: list[str] = []
    week_token = f"{year}-W{week:02d}"
    lines.append(f"# [宣发周报] {week_token}（{start.date().isoformat()} - "
                 f"{(end - timedelta(days=1)).date().isoformat()}）")
    lines.append("")

    # 1) 本周发文 × 账号矩阵
    lines.append(
        f"## 本周发文：{n_articles} 篇 × {n_accounts} 账号 = {len(jobs)} 条投放"
    )
    lines.append("")

    # 2) 总曝光
    lines.append(f"## 总曝光：{total_views}（同比 N/A，需历史数据沉淀后计算）")
    lines.append("")

    # 3) 总互动
    lines.append(f"## 总互动：{total_engagement}（同比 N/A）")
    lines.append("")

    # 4) 主题 ROI 排行
    lines.append("## 主题 ROI 排行")
    if topic_roi:
        for i, r in enumerate(topic_roi, 1):
            lines.append(
                f"{i}. {r['name']}：posts={r['posts']}, views={r['views']}, "
                f"CPM {r['cpm']}, 互动率 {r['rate']}%"
            )
    else:
        lines.append("- （本周无发布）")
    lines.append("")

    # 5) 爆款 TOP 3
    lines.append("## 爆款 TOP 3（按互动率排序）")
    if top3:
        for i, j in enumerate(top3, 1):
            art = art_map.get(j.article_id)
            title = art.title if art else f"<article#{j.article_id}>"
            topic = topic_map.get(art.topic_id) if art else None
            tname = topic.name if topic else "-"
            acct = acct_map.get(j.account_id)
            aname = acct.nickname if acct else f"acct#{j.account_id}"
            lines.append(f"{i}. 《{title}》| {tname} | {j.platform} | {aname}")
    else:
        lines.append("- （本周无发布）")
    lines.append("")

    # 6) 账号矩阵表现
    lines.append("## 账号矩阵表现")
    if acct_ranked:
        top_id, top_st = acct_ranked[0]
        bot_id, bot_st = acct_ranked[-1]
        top_a = acct_map.get(top_id)
        bot_a = acct_map.get(bot_id)
        lines.append(
            f"- 最高 ROI 账号：{top_a.nickname if top_a else top_id}"
            f"（posts={top_st['posts']}, 互动={top_st['eng']}，建议加投）"
        )
        lines.append(
            f"- 最低 ROI 账号：{bot_a.nickname if bot_a else bot_id}"
            f"（posts={bot_st['posts']}, 互动={bot_st['eng']}，建议人设 review）"
        )
    else:
        lines.append("- （本周无发布）")
    lines.append("")

    # 7) product_features 热度
    lines.append("## product_features 热度（按 Topic.keywords 桶）")
    if feat_rank:
        for kw, posts in feat_rank:
            views = feat_counter_views[kw]
            lines.append(f"- {kw}：{posts} 篇 / {views} 曝光")
    else:
        lines.append("- （本周无可统计 keywords）")
    lines.append("")

    # 8) prompt 归因（MVP: out of scope, follow-up）
    lines.append("## prompt 归因")
    lines.append("- 高分模式：(out of scope, follow-up — 待 L3 prompt 元数据落库)")
    lines.append("- 低分模式：(out of scope, follow-up — 待 L3 prompt 元数据落库)")
    lines.append("")

    # 9) 下周计划（基于本周排行自动生成的建议）
    lines.append("## 下周计划")
    if topic_roi:
        best = topic_roi[0]
        lines.append(f"- 复用模式：加投 [{best['name']}]（本周互动率 {best['rate']}%）")
    else:
        lines.append("- 复用模式：（本周无数据，待下周回填）")
    if feat_rank:
        kws = ", ".join(kw for kw, _ in feat_rank[:3])
        lines.append(f"- 重点 push 的 product_features：{kws}")
    else:
        lines.append("- 重点 push 的 product_features：（待数据沉淀）")
    lines.append("- 实验方向：(由运营 review 周报后填入)")
    lines.append("")

    return "\n".join(lines)


def write_weekly_report(
    year: int, week: int, out_dir: Path | str = "./reports"
) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"weekly-{year}-W{week:02d}.md"
    with session_scope() as s:
        md = build_weekly_report(s, year, week)
    path.write_text(md, encoding="utf-8")
    return path


async def run_weekly_report_job() -> dict:
    """cron 入口 — 默认当前 ISO 周（按 UTC date）。"""
    from ..notify import report_ready

    today = datetime.utcnow().date()
    iso = today.isocalendar()
    year, week = iso[0], iso[1]
    try:
        path = write_weekly_report(year, week)
        report_ready("weekly", str(path.resolve()))
        return {
            "ok": True, "kind": "weekly",
            "week": f"{year}-W{week:02d}", "path": str(path),
        }
    except Exception as e:
        return {
            "ok": False, "kind": "weekly",
            "week": f"{year}-W{week:02d}", "error": str(e),
        }
