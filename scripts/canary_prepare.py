#!/usr/bin/env python
"""Canary runbook for the API/CLI publishers (B站 / 抖音 / 快手), in two commands.

A canary has two halves and they are kept apart on purpose:

``prepare``
    Stops before anything is published. It checks that the switch is really on,
    creates a dedicated account with the grant encrypted at rest, validates the
    grant, and reports the real reason when it does not work.

``publish``
    The public write. It goes through the ordinary pipeline (article -> approve ->
    distribute -> job) so every gate, receipt and audit row applies exactly as it
    would for real traffic. It refuses to run unless told to.

Neither command turns a switch on.

    scripts/canary_prepare.py prepare kuaishou --name ks-canary < credential.json
    scripts/canary_prepare.py prepare bilibili --name bili-canary
    scripts/canary_prepare.py publish  bilibili --account-id 3 --video data/canary.mp4

``credential.json`` holds the platform grant, e.g. for Kuaishou::

    {"app_id": "...", "app_secret": "...", "refresh_token": "..."}

It is read from stdin, so the secret never appears in argv, shell history or the
process list.

Why ``prepare`` calls the adapter's ``login()`` directly instead of
``POST /accounts/{id}/login``: the API answers every failed login with the same
generic sentence on purpose (third-party adapters may put anything in their error
text). Here the built-in adapter runs on the operator's own machine, so its reason
can be shown, e.g. "快手 refresh_token 已过期（result 100200113）；请重新完成用户授权".

Exit codes: 0 = passed, 1 = not passed (nothing was published), 2 = usage or
environment problem.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

# This script is read by a person at a terminal, often through a pipe. Progress
# goes to stdout and findings to stderr, and without this the two streams would
# be buffered differently and interleave out of order.
for _stream in (sys.stdout, sys.stderr):
    getattr(_stream, "reconfigure", lambda **_: None)(line_buffering=True)

# platform -> (switch, doctor capability, PublisherKind value, required credential keys)
SPECS: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "kuaishou": (
        "KUAISHOU_OPEN_API_ENABLED",
        "kuaishou_open_api",
        "kuaishou_open_api",
        ("app_id", "app_secret", "refresh_token"),
    ),
    "douyin": (
        "DOUYIN_OPEN_API_ENABLED",
        "douyin_open_api",
        "douyin_open_api",
        ("client_key", "client_secret", "refresh_token"),
    ),
    "bilibili": ("BILITOOL_ENABLED", "bilitool", "bilitool", ()),
}

CANARY_TITLE = "canary"

# bilitool's own upload lines; the adapter accepts exactly these.
_BILITOOL_CDN_LINES = ("qn", "bldsa", "ws", "bda2", "tx")


def fail(message: str) -> None:
    print(f"  ✗ {message}", file=sys.stderr)


def read_doctor() -> dict:
    """``doctor --json`` in a subprocess, so its flags come from the real settings."""
    proc = subprocess.run(
        [sys.executable, "-m", "ai_ops.cli", "doctor", "--json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if not proc.stdout.strip():
        raise RuntimeError("doctor 没有返回结果：" + (proc.stderr or "").strip()[-300:])
    return json.loads(proc.stdout)


def check_switch(platform: str, spec: tuple) -> bool:
    """The switch must be on, and the platform must resolve to the API/CLI adapter first."""
    switch, capability, kind, _required = spec
    report = read_doctor()
    by_id = {check["id"]: check for check in report["checks"]}
    safety = by_id.get("safety.publication") or {}
    live = (safety.get("details") or {}).get("enabled_capabilities") or []
    if capability not in live:
        fail(
            f"{switch} 没有生效（doctor 看到的 live capabilities: {live or '无'}）。"
            "本脚本不会替你打开写主链路的开关：请在与本脚本相同的环境里设置它后重试。"
        )
        return False
    print(f"  ✓ {switch} 已生效，doctor 把 {capability} 列为 live capability")

    from ai_ops.core.enums import Platform
    from ai_ops.publishers.registry import default_registry

    publishers = default_registry.resolve(Platform(platform))
    names = [type(publisher).__name__ for publisher in publishers]
    if not publishers or getattr(publishers[0].kind, "value", publishers[0].kind) != kind:
        fail(f"{platform} 解析到的发布器顺序是 {names}，排第一的不是 {kind}。")
        return False
    print(f"  ✓ {platform} 的发布器顺序: {names}")

    return True


def create_canary_account(platform: str, nickname: str, spec: tuple) -> int | None:
    """Create the dedicated account; the grant is read from stdin and encrypted at rest."""
    from ai_ops.accounts.manager import create_account
    from ai_ops.accounts.store import get_store
    from ai_ops.core.db import session_scope
    from ai_ops.core.enums import Platform
    from ai_ops.core.models import Account
    from ai_ops.core.schemas import AccountIn
    from sqlalchemy import select

    credential: dict = {}
    if spec[3]:
        raw = sys.stdin.read()
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            fail("标准输入不是合法 JSON（凭证文件的格式不对）")
            return None
        if not isinstance(loaded, dict):
            fail("凭证必须是一个 JSON 对象")
            return None
        credential = loaded
        missing = [key for key in spec[3] if not credential.get(key)]
        if missing:
            fail("凭证缺少字段: " + ", ".join(missing))
            return None

    with session_scope() as session:
        existing = session.execute(
            select(Account).where(
                Account.platform == Platform(platform),
                Account.nickname == nickname,
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Re-running prepare must not pile up dead accounts: the same
            # nickname means the same canary account, with the grant replaced.
            existing.encrypted_credential = (
                get_store().encrypt(credential) if credential else b""
            )
            account_id = existing.id
        else:
            account_id = create_account(
                session,
                AccountIn(
                    platform=Platform(platform),
                    nickname=nickname,
                    credential_plain=credential,
                    daily_quota=1,
                ),
            ).id
    print(f"  ✓ 账号 #{account_id}（{platform} / {nickname}），凭证已加密落库")
    return account_id


async def _verify_login(platform: str, account_id: int) -> bool:
    """Validate the grant, and persist what ``login()`` rotated.

    Under the same cross-process account lock the API's login endpoint takes,
    because a refresh that is not persisted is worse than no refresh: Kuaishou's
    refresh token is single-use and the old one dies the moment the platform
    answers, so a rotated token left only in memory would strand the account.
    """
    from ai_ops.accounts.manager import get_credential, update_health
    from ai_ops.accounts.store import get_store
    from ai_ops.config import settings
    from ai_ops.core.db import session_scope
    from ai_ops.core.enums import AccountHealth, Platform
    from ai_ops.core.models import Account
    from ai_ops.publishers.registry import default_registry
    from ai_ops.runtime.account_lease import AccountOperationLease, AccountOperationLeaseTimeout

    publisher = default_registry.resolve(Platform(platform))[0]
    with session_scope() as session:
        if session.get(Account, account_id) is None:
            fail(f"账号 #{account_id} 不存在")
            return False
    if getattr(publisher.kind, "value", publisher.kind) == "bilitool":
        # The adapter runs only the account's own venv binary, never the PATH
        # copy that ``doctor``'s adapter.bilitool probe looks at, so ask it directly.
        binary = publisher._resolved_binary(account_id)
        if binary is None:
            home = Path(settings.bilitool_profile_root).expanduser() / f"account_{account_id}"
            fail(
                f"账号 #{account_id} 的隔离 venv 里没有 bilitool：{home / 'venv'}\n"
                "        按 docs/publishing-sop.md §三-E 建 venv 并安装 bilitool==0.1.3"
            )
            return False
        print(f"  ✓ 将被执行的 bilitool: {binary}")
    try:
        async with AccountOperationLease(
            account_id,
            timeout_seconds=float(settings.account_operation_lock_timeout_seconds),
        ):
            with session_scope() as session:
                try:
                    credential = get_credential(session, account_id)
                except ValueError:
                    # Same as the API's login route: adapters whose state lives on
                    # disk (bilitool's cookie file) have no stored credential.
                    credential = {}
            ok = await publisher.login(account_id, credential)
            # ``health_check`` refreshes again for the token-based adapters, and on
            # Kuaishou every refresh rotates the single-use token, so the probe must
            # run inside the lock too and the last rotated grant is what gets stored.
            health = await publisher.health_check(account_id, credential) if ok else None
            # Persist before releasing the lock, exactly like the API endpoint does,
            # so no worker or health probe can read a half-updated grant.
            if ok and credential:
                with session_scope() as session:
                    account = session.get(Account, account_id)
                    account.encrypted_credential = get_store().encrypt(credential)
    except AccountOperationLeaseTimeout:
        fail("账号正被其他操作占用（worker / 健康探测），稍后重试")
        return False

    if not ok:
        reason = getattr(publisher, "last_login_error", None) or "适配器没有给出原因"
        fail("登录未通过：" + reason)
        return False

    if health is AccountHealth.HEALTHY:
        with session_scope() as session:
            update_health(session, account_id, health)
        print("  ✓ 登录可用，health_check = HEALTHY（已写入账号健康状态）")
        return True
    fail(
        "登录成功但 health_check 没有给出 HEALTHY（"
        + getattr(health, "value", str(health))
        + "）。不发布，先排查。"
    )
    return False


def verify_login(platform: str, account_id: int) -> bool:
    return asyncio.run(_verify_login(platform, account_id))


def _find_or_create_topic(platform: str) -> int:
    """One topic per platform for canary articles; reused across runs."""
    from ai_ops.content.manager import create_topic
    from ai_ops.core.db import session_scope
    from ai_ops.core.enums import Platform
    from ai_ops.core.models import Topic
    from ai_ops.core.schemas import TopicIn
    from sqlalchemy import select

    name = f"canary-{platform}"
    with session_scope() as session:
        topic = session.execute(select(Topic).where(Topic.name == name)).scalar_one_or_none()
        if topic is not None:
            return topic.id
        return create_topic(
            session,
            TopicIn(name=name, category="general", target_platforms=[Platform(platform)]),
        ).id


def _publish_blocking(platform: str, account_id: int, video: str, *, extra: dict | None = None) -> int:
    """The public write, through the ordinary pipeline: no gate is skipped."""
    from ai_ops.content import distributor
    from ai_ops.content.manager import attach_asset, create_article
    from ai_ops.core.db import session_scope
    from ai_ops.core.enums import (
        AccountHealth,
        AssetType,
        ContentType,
        JobStatus,
        Platform,
    )
    from ai_ops.core.models import Account
    from ai_ops.core.models import PublishJob
    from ai_ops.core.schemas import ArticleIn, AssetRef
    from ai_ops.scheduler.worker import execute_job

    expected_kind = SPECS[platform][2]
    video_path = Path(video).expanduser().resolve(strict=True)
    topic_id = _find_or_create_topic(platform)
    with session_scope() as session:
        account = session.get(Account, account_id)
        if account is None or Platform(account.platform) is not Platform(platform):
            fail(f"账号 #{account_id} 不存在，或不是 {platform} 账号")
            return 2
        if AccountHealth(account.health) is AccountHealth.EXPIRED:
            fail(f"账号 #{account_id} 的健康状态是 EXPIRED，先重新 prepare")
            return 1
        article_id = create_article(
            session,
            ArticleIn(
                topic_id=topic_id,
                title=CANARY_TITLE,
                body="canary：由 scripts/canary_prepare.py 发出的验证内容，可删除。",
                content_type=ContentType.VIDEO,
                target_platforms=[Platform(platform)],
                target_account_ids=[account_id],
                extra=dict(extra or {}),
            ),
        ).id
        attach_asset(
            session,
            article_id,
            AssetRef(asset_type=AssetType.VIDEO, local_path=str(video_path)),
        )
        distributor.approve(session, article_id)
        job_id = distributor.distribute(session, article_id, account_ids=[account_id])[0].id
    print(f"  ✓ 素材 #{article_id} 已审批并分发给账号 #{account_id}，job #{job_id}")

    result = asyncio.run(execute_job(job_id))
    with session_scope() as session:
        job = session.get(PublishJob, job_id)
        actual_kind = job.publisher_kind
        status = str(JobStatus(job.status).value)
        post_id = job.platform_post_id
        raw = dict(job.raw_response or {})
    print(f"\n▎结果：job #{job_id} status={status}，实际使用的发布器 = {actual_kind or '（未到达任何发布器）'}")
    if actual_kind and actual_kind != expected_kind:
        # The worker falls through to the next Publisher (the browser path) when
        # the API/CLI adapter fails before sending anything. Such a run says
        # nothing about the adapter being tested, even when it succeeds.
        print(
            f"  ✗ 这次不是 {expected_kind} 发的，而是回退到了 {actual_kind}；"
            "它不证明 API/CLI 路径可用。"
        )
        return 1
    if result.success:
        print(f"  ✓ 发布成功，平台作品 ID = {post_id}。请到 {platform} 平台端人工确认。")
        return 0
    print(f"  ✗ 未发布成功：{result.error}")
    uncertain = "outcome_uncertain" in (raw or {})
    advice = {
        "retrying": "worker 会按重试节奏自动再试；养号期、发布间隔也表现为这个状态。",
        "dead": "重试预算已用完，账号健康可能已被降级。",
        "failed": "不会自动重试。平台已确认拒绝或未发出，可以修正后重新 publish。",
    }.get(status, "")
    if uncertain:
        advice = "平台结果未知：平台端可能已有作品，先人工核对再决定是否重发。"
    if advice:
        print("    " + advice)
    return 1


def run_publish(
    platform: str,
    account_id: int,
    video: str,
    *,
    cdn: str = "",
    copyright_value: int | None = None,
) -> int:
    spec = SPECS[platform]
    print(f"▎canary 发布：{platform} / 账号 #{account_id}（这一步会公开发布）")
    if cdn and platform != "bilibili":
        fail("--cdn 只适用于 bilibili")
        return 2
    extra: dict = {}
    if cdn:
        extra["bilibili_cdn_line"] = cdn
    if copyright_value is not None:
        extra["bilibili_copyright"] = copyright_value
    if not check_switch(platform, spec):
        return 1
    if not verify_login(platform, account_id):
        return 1
    return _publish_blocking(platform, account_id, video, extra=extra)


def run_prepare(platform: str, nickname: str) -> int:
    spec = SPECS[platform]
    print(f"▎canary 准备：{platform}（只准备到「登录可用」，不发布任何内容）")
    if not check_switch(platform, spec):
        return 1
    account_id = create_canary_account(platform, nickname, spec)
    if account_id is None:
        return 1
    if not verify_login(platform, account_id):
        print(
            f"\n  账号 #{account_id} 已建好并保留；修好上面的问题后重新验证：\n"
            f"    {Path(sys.argv[0]).name} prepare {platform} --name {nickname} --account-id {account_id}",
            file=sys.stderr,
        )
        return 1
    print("\n▎准备完成。发布那一步会公开写平台，脚本不会自动执行；命令：")
    print(
        f"  {Path(sys.argv[0]).name} publish {platform} "
        f"--account-id {account_id} --video <asset root 内的 mp4>",
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="canary_prepare.py",
        description="API/CLI 发布器的 canary：prepare 只验证到登录可用；publish 才公开发布。",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="确认开关、建账号、验证登录；不发布")
    prepare.add_argument("platform", choices=sorted(SPECS))
    prepare.add_argument("--name", required=True, help="专用测试账号在库里的名字")
    prepare.add_argument(
        "--account-id",
        type=int,
        help="复用已有账号（修好问题后重新验证），不再新建",
    )
    publish = commands.add_parser("publish", help="公开发布一条 canary；需要 --yes")
    publish.add_argument("platform", choices=sorted(SPECS))
    publish.add_argument("--account-id", type=int, required=True)
    publish.add_argument("--video", required=True, help="asset root 内的本地视频")
    publish.add_argument("--yes", action="store_true", help="确认公开发布")
    publish.add_argument(
        "--copyright",
        choices=("self", "repost"),
        default="",
        help="bilibili 专用：self=自制，repost=转载；不填用适配器默认（转载）",
    )
    publish.add_argument(
        "--cdn",
        choices=sorted(_BILITOOL_CDN_LINES),
        default="",
        help="bilibili 专用：指定上传线路，跳过 bilitool 的线路自动探测",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "prepare":
        if args.account_id is not None:
            print(f"▎canary 重新验证：{args.platform} / 账号 #{args.account_id}")
            spec = SPECS[args.platform]
            if not check_switch(args.platform, spec):
                return 1
            return 0 if verify_login(args.platform, args.account_id) else 1
        return run_prepare(args.platform, args.name)
    if not args.yes:
        fail("publish 会公开发布内容；确认后加 --yes")
        return 2
    copyright_value = {"self": 1, "repost": 2}.get(args.copyright, None)
    if copyright_value is not None and args.platform != "bilibili":
        fail("--copyright 只适用于 bilibili")
        return 2
    return run_publish(
        args.platform, args.account_id, args.video, cdn=args.cdn, copyright_value=copyright_value
    )


if __name__ == "__main__":
    sys.exit(main())
