"""jobhunt 子命令组：`python -m ai_ops.cli jobhunt ...`。

P0 只有 parse-resume。后续 P1+ 会加 crawl / match / list-candidates / apply 等。
"""
from __future__ import annotations

import asyncio
import json

import typer

jobhunt_app = typer.Typer(help="求职投递专题（简历分析 → 全平台自动投递）")


def _ev(x) -> str:
    """显示用：str-enum 列经 DB 往返后会变回纯 str，统一取展示值（有 .value 取之，否则原样）。"""
    return x.value if hasattr(x, "value") else str(x)


@jobhunt_app.command("parse-resume")
def cmd_parse_resume(
    path: str = typer.Argument(..., help="简历文件路径（.pdf/.docx/.txt/.md）"),
    name: str = typer.Option("", "--name", "-n", help="给这份简历起的标签，缺省取姓名/文件名"),
    no_active: bool = typer.Option(
        False, "--no-active", help="不把这份设为当前主用简历"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="只解析打印结构化结果，不落库"
    ),
):
    """解析简历并入库（落 Asset + ResumeProfile）。"""
    from ..core.db import session_scope
    from .resume_parser import parse_resume_file
    from .service import ingest_resume

    if dry_run:
        _, structured = asyncio.run(parse_resume_file(path))
        typer.echo(json.dumps(structured, ensure_ascii=False, indent=2))
        typer.echo("\n[dry-run] 未落库。")
        return

    # ingest_resume 是 async；用一个外层 async 包住 session 写入，确保事务在协程内完成。
    async def _ingest() -> dict:
        with session_scope() as s:
            profile = await ingest_resume(s, path, name=name or None, set_active=not no_active)
            return {
                "id": profile.id,
                "name": profile.name,
                "raw_asset_id": profile.raw_asset_id,
                "target_titles": profile.target_titles,
                "expected_cities": profile.expected_cities,
                "skills": profile.skills,
                "search_keywords": profile.search_keywords,
                "is_active": profile.is_active,
            }

    result = asyncio.run(_ingest())
    typer.echo("OK: 简历已入库")
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@jobhunt_app.command("list-resumes")
def cmd_list_resumes():
    """列出已入库的简历。"""
    from ..core.db import session_scope
    from sqlalchemy import select
    from .models import ResumeProfile

    with session_scope() as s:
        rows = s.scalars(select(ResumeProfile).order_by(ResumeProfile.id)).all()
        if not rows:
            typer.echo("（暂无简历，先跑 parse-resume）")
            return
        for r in rows:
            flag = "★" if r.is_active else " "
            typer.echo(
                f"{flag} #{r.id}  {r.name}  "
                f"目标={r.target_titles}  城市={r.expected_cities}  "
                f"薪资={r.expected_salary_min}-{r.expected_salary_max}"
            )


@jobhunt_app.command("add-account")
def cmd_add_account(
    nickname: str = typer.Argument(..., help="账号备注名"),
    cookie_file: str = typer.Option("", "--cookie-file", "-c", help="浏览器导出的 cookie JSON 文件路径"),
    cdp: bool = typer.Option(
        False, "--cdp", help="CDP 模式账号：不存 cookie，登录态来自真 Chrome（配 BROWSER_CDP_URL）"
    ),
    board: str = typer.Option("boss", "--board", "-b", help="招聘平台：boss/zhilian/liepin/job51"),
    daily_quota: int = typer.Option(30, "--daily-quota", help="日投递上限（Boss 保守默认 30）"),
):
    """登记招聘平台账号。两种：

    1. cookie 模式（--cookie-file）：导入浏览器导出的 cookie，Fernet 加密落库（需 FERNET_KEY）。
       文件内容为数组 [{"name","value","domain","path"}, ...] 或 {"cookies": [...]}。
    2. CDP 模式（--cdp）：不存 cookie，只建账号行记配额/绑定；登录态运行时从真 Chrome 取。
    """
    import json
    from pathlib import Path

    from ..core.db import session_scope
    from .accounts import create_account, create_cdp_account
    from .enums import JobBoard

    if cdp:
        with session_scope() as s:
            acc = create_cdp_account(s, JobBoard(board), nickname, daily_quota=daily_quota)
            typer.echo(
                f"OK: CDP 账号 #{acc.id} [{board}] {nickname} 已登记（无 cookie，登录态走真 Chrome）"
            )
        return

    if not cookie_file:
        raise typer.BadParameter("非 CDP 模式需 --cookie-file，或加 --cdp 走真 Chrome 登录态")

    data = json.loads(Path(cookie_file).read_text(encoding="utf-8"))
    cookies = data.get("cookies") if isinstance(data, dict) else data
    if not isinstance(cookies, list) or not cookies:
        raise typer.BadParameter("cookie 文件需是非空数组，或含非空 cookies 字段的对象")

    with session_scope() as s:
        acc = create_account(
            s, JobBoard(board), nickname, cookies, daily_quota=daily_quota
        )
        typer.echo(f"OK: 招聘账号 #{acc.id} [{board}] {nickname} 已加密入库（{len(cookies)} 条 cookie）")


@jobhunt_app.command("list-accounts")
def cmd_list_accounts(
    board: str = typer.Option("", "--board", "-b", help="按平台筛选；空=全部"),
):
    """列出招聘平台账号。"""
    from ..core.db import session_scope
    from .accounts import list_accounts
    from .enums import JobBoard

    with session_scope() as s:
        rows = list_accounts(s, JobBoard(board) if board else None)
        if not rows:
            typer.echo("（暂无招聘账号，先 add-account）")
            return
        for a in rows:
            has_cred = "有凭证" if a.encrypted_credential else "无凭证"
            typer.echo(
                f"#{a.id} [{_ev(a.board)}] {a.nickname}  健康={_ev(a.health)}  "
                f"日上限={a.daily_quota}  {has_cred}"
            )


@jobhunt_app.command("crawl-match")
def cmd_crawl_match(
    resume_id: int = typer.Option(0, "--resume-id", "-r", help="用哪份简历；0=当前主用简历"),
    board: str = typer.Option("boss", "--board", "-b", help="招聘平台：boss/zhilian/liepin/job51"),
    min_score: float = typer.Option(60.0, "--min-score", help="进候选池的最低匹配分(0-100)"),
    limit: int = typer.Option(20, "--limit", help="本次最多采集岗位数"),
    fake: bool = typer.Option(
        False, "--fake", help="用 FakeApplier 离线演练整条管道（不碰真平台）"
    ),
):
    """采集岗位 → 匹配打分 → 过阈值落候选池(DRAFT)。**不直投**，等你勾选审核。"""
    import asyncio

    from sqlalchemy import select

    from ..core.db import session_scope
    from .enums import JobBoard
    from .matcher import JobMatcher
    from .greeting import GreetingGenerator
    from .models import ResumeProfile
    from .pipeline import JobHuntPipeline
    from .schemas import JobQuery

    if fake:
        from .appliers.fake import FakeApplier
        applier = FakeApplier()
    else:
        from .appliers.registry import default_registry
        applier = default_registry.first(JobBoard(board))

    async def _run():
        with session_scope() as s:
            if resume_id:
                resume = s.get(ResumeProfile, resume_id)
            else:
                resume = s.scalar(
                    select(ResumeProfile).where(ResumeProfile.is_active.is_(True))
                )
            if resume is None:
                raise typer.BadParameter("找不到简历（先 parse-resume，或用 --resume-id 指定）")

            query = JobQuery(
                keywords=list(resume.search_keywords or resume.target_titles or []),
                city=(resume.expected_cities or [""])[0],
                salary_min=resume.expected_salary_min,
                limit=limit,
            )
            pipe = JobHuntPipeline(applier, JobMatcher(), GreetingGenerator())
            return await pipe.run(s, resume, query, min_score=min_score)

    out = asyncio.run(_run())
    typer.echo(
        f"采集 {out.searched} / 打分 {out.scored} / "
        f"落候选池 {out.staged} / 分数不够 {out.skipped_below} / 已存在 {out.skipped_dup}"
    )
    if out.staged_ids:
        typer.echo(f"新候选 Application id: {out.staged_ids}")
    typer.echo("→ 用 `jobhunt candidates` 看候选池，勾选后（P2）才会真投。")


@jobhunt_app.command("candidates")
def cmd_candidates(
    status: str = typer.Option("draft", "--status", "-s", help="按状态筛选：draft/ready/applied..."),
    limit: int = typer.Option(50, "--limit"),
):
    """查看候选池（默认 DRAFT 待勾选），含匹配分和打招呼语预览。"""
    from sqlalchemy import select

    from ..core.db import session_scope
    from .enums import ApplicationStatus
    from .models import Application, JobMatch, JobPosting

    with session_scope() as s:
        q = (
            select(Application)
            .where(Application.status == ApplicationStatus(status))
            .order_by(Application.id.desc())
            .limit(limit)
        )
        rows = s.scalars(q).all()
        if not rows:
            typer.echo(f"（候选池中无 {status} 记录）")
            return
        for a in rows:
            job = s.get(JobPosting, a.job_id)
            m = s.scalar(
                select(JobMatch).where(
                    JobMatch.resume_id == a.resume_id, JobMatch.job_id == a.job_id
                )
            )
            score = f"{m.score:.0f}" if m else "?"
            typer.echo(
                f"#{a.id} [{_ev(a.board)}] 分{score} {job.title} @ {job.company}（{job.location}）"
            )
            typer.echo(f"     招呼语: {a.greeting[:60]}{'…' if len(a.greeting) > 60 else ''}")


@jobhunt_app.command("approve")
def cmd_approve(
    ids: list[int] = typer.Argument(None, help="勾选这些候选 Application id 进 READY"),
    min_score: float = typer.Option(
        None, "--min-score", help="批量：把匹配分≥阈值的 DRAFT 一并放行（不给 ids 时生效）"
    ),
    board: str = typer.Option("", "--board", "-b", help="只放行该平台；空=全部"),
):
    """人工闸：把候选池里的 DRAFT 勾选成 READY（待投）。ids 与 --min-score 至少给一个。"""
    from ..core.db import session_scope
    from .apply_service import approve_applications
    from .enums import JobBoard

    if not ids and min_score is None:
        raise typer.BadParameter("给具体 id（如 `approve 3 5 7`）或 `--min-score 75`，避免误放全部")

    with session_scope() as s:
        promoted = approve_applications(
            s,
            ids=list(ids) if ids else None,
            min_score=min_score,
            board=JobBoard(board) if board else None,
        )
    if not promoted:
        typer.echo("（没有匹配的 DRAFT 被放行）")
        return
    typer.echo(f"OK: {len(promoted)} 条已进 READY 待投：{promoted}")
    typer.echo("→ 用 `jobhunt execute` 真投（先确认已 add-account 导入登录态）。")


@jobhunt_app.command("execute")
def cmd_execute(
    board: str = typer.Option("boss", "--board", "-b", help="投递平台：boss/zhilian/liepin/job51"),
    account_id: int = typer.Option(0, "--account-id", "-a", help="指定用哪个账号；0=自动选剩余配额最多的"),
    limit: int = typer.Option(10, "--limit", help="本轮最多投几条"),
    fake: bool = typer.Option(
        False, "--fake", help="用 FakeApplier 离线演练（永远成功，不碰真平台）"
    ),
):
    """真投递：把 READY 候选投出去（Boss=进会话发招呼语），落 APPLIED/FAILED/DEAD。"""
    import asyncio

    from ..core.db import session_scope
    from .apply_service import execute_applications
    from .enums import JobBoard

    if fake:
        from .appliers.fake import FakeApplier
        applier = FakeApplier()
    else:
        from .appliers.registry import default_registry
        applier = default_registry.first(JobBoard(board))

    async def _run():
        with session_scope() as s:
            return await execute_applications(
                s,
                applier=applier,
                board=JobBoard(board),
                account_id=account_id or None,
                limit=limit,
            )

    rep = asyncio.run(_run())
    typer.echo(
        f"投出 {rep.applied} / 失败 {rep.failed} / 放弃(DEAD) {rep.dead} / "
        f"无账号跳过 {rep.skipped_no_account} / 配额满跳过 {rep.skipped_quota}"
    )
    if rep.applied_ids:
        typer.echo(f"已投 Application id: {rep.applied_ids}")
    for app_id, err in rep.errors:
        typer.echo(f"  ✗ #{app_id}: {err}")
