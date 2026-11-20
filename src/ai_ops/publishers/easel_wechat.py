"""微信公众号 Easel 会话版 Publisher — 薄壳 wrapper，复用 vendored Easel 扫码会话。

为什么需要：自建 ``WechatMpPublisher`` 走 persistent-context + 占位 selector，
尚未真发校准；而 vendored Easel（vendor/easel，Apache-2.0，上游 commit 见
``EASEL_UPSTREAM_COMMIT``）的 ``weixin_mp_stats.py`` 已端到端跑通「后台扫码会话」：
免 AppID/AppSecret、免 IP 白名单、未认证个人号可用，publish=建草稿、stats 真实读回。
本适配器只做翻译与失败语义治理，不重写发布核心逻辑（反爬/上传归属 Easel）。

阶段 1：仅「保存为草稿」——**绝不触发群发/freepublish**。与 WechatMpPublisher 同一
纪律：确认草稿=外部副作用但非公开发布，故 success=False/effect_applied=True/
retryable=False，Article 永不被误标 PUBLISHED，fallback 不会制造重复草稿。

默认关闭（``easel_wechat_publisher_enabled``），与其余 canary 一致；启用前须完成
专用账号 canary 与 readback 证据卡。
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from ..config import settings
from ..core.enums import AccountHealth, AssetType, ContentType, Platform
from ..core.schemas import PublishContent, PublishResult
from ..runtime.subprocess import communicate_bounded
from .base import AgentContractAssetRule, AgentContractRendererDescriptor, PublisherBase

# 版本门禁：锁定 vendored Easel 上游 commit，升级须重新审计并更新此常量。
EASEL_UPSTREAM_COMMIT = "011e10d"
EASEL_SCRIPT_REL = Path("vendor/easel/skills/shared/scripts/weixin_mp_stats.py")


def _repo_root() -> Path:
    # src/ai_ops/publishers/easel_wechat.py -> parents[3] = repo root
    return Path(__file__).resolve().parents[3]


def easel_script_path() -> Path:
    return _repo_root() / EASEL_SCRIPT_REL


async def run_easel_script(args: list[str], timeout: float = 120.0) -> tuple[int, str, str]:
    """执行 vendored Easel 脚本，返回 (returncode, stdout, stderr)。

    独立成模块级函数便于单测 monkeypatch（不真spawn浏览器/子进程）。
    """
    script = easel_script_path()
    if not script.is_file():
        return (127, "", f"easel script missing: {script}")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            communicate_bounded(proc), timeout=timeout
        )
    except asyncio.TimeoutError:
        from ..runtime.subprocess import stop_process_group

        await stop_process_group(proc)
        return (-1, "", "timeout")
    return (proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


def _parse_json(stdout: str) -> dict | None:
    try:
        data = json.loads(stdout.strip().splitlines()[-1])
        return data if isinstance(data, dict) else None
    except (ValueError, IndexError):
        return None


class EaselWechatPublisher(PublisherBase):
    platform = Platform.WECHAT_MP
    kind = "easel_wechat_oa"  # namespaced 字符串，持久化 job 列为有界字符串
    supports_metrics = True

    agent_contract_renderer_descriptor = AgentContractRendererDescriptor(
        renderer_id="easel_wechat_oa_draft",
        contract_version="1",
        adapter_version=EASEL_UPSTREAM_COMMIT,
        platform=Platform.WECHAT_MP,
        publisher_kind="easel_wechat_oa",
        accepted_extra_keys=("digest",),
        accepts_tags=False,
        requires_external_account_id=False,
        asset_rules=(AgentContractAssetRule(asset_type=AssetType.IMAGE, min_count=1, max_count=1),),
    )

    def render_agent_contract_payload(self, content: PublishContent) -> dict[str, object]:
        # 纯投影：不含凭证/宿主路径，供审批摘要与写前重算复用。
        return {
            "title": content.title,
            "body": content.body,
            "digest": str(content.extra.get("digest", "")),
            "cover_asset_count": len(content.images),
        }

    async def login(self, account_id: int, credential: dict) -> bool:
        # 扫码须本人操作（Easel UI 递码）；这里只校验会话是否已有效。
        rc, out, _ = await run_easel_script(["whoami"])
        data = _parse_json(out)
        return rc == 0 and bool(data and data.get("loggedIn"))

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        rc, out, _ = await run_easel_script(["whoami"])
        if rc != 0:
            return AccountHealth.UNKNOWN
        data = _parse_json(out)
        if data is None:
            return AccountHealth.UNKNOWN
        return AccountHealth.HEALTHY if data.get("loggedIn") else AccountHealth.EXPIRED

    async def collect_metrics(self, post_id: str, post_url: str | None, credential: dict) -> dict:
        rc, out, err = await run_easel_script(["stats"])
        data = _parse_json(out)
        if rc != 0 or data is None:
            raise NotImplementedError(f"easel stats unavailable: {err[:200]}")
        # 草稿（无公开 URL）没有公开阅读/点赞/评论数据：fail-closed 跳过，绝不伪造计数。
        # 已发表文章的逐篇 likes/comments 需上游 stats 扩展后才能归一化（Phase C），
        # 当前同样跳过而非用账号级数据冒充逐篇指标。
        return {"skipped": True}

    async def publish(
        self, account_id: int, credential: dict, content: PublishContent
    ) -> PublishResult:
        if content.content_type == ContentType.VIDEO:
            return PublishResult(success=False, retryable=False, error="公众号图文适配器不支持视频")
        if not (content.title or "").strip():
            return PublishResult(success=False, retryable=False, error="标题为空")
        if not content.images or not Path(content.images[0]).is_file():
            return PublishResult(success=False, retryable=False, error="公众号草稿需有效封面图")
        if not easel_script_path().is_file():
            return PublishResult(success=False, retryable=False, error="vendored Easel 脚本缺失")

        # 写前预检登录态：未登录=确定失败，无副作用，不消耗重试。
        rc, out, _ = await run_easel_script(["whoami"])
        who = _parse_json(out)
        if rc != 0 or not (who and who.get("loggedIn")):
            return PublishResult(success=False, retryable=False, error="Easel 会话未登录，请先扫码")

        import markdown

        html = markdown.markdown(content.body or "", extensions=["fenced_code", "tables", "nl2br"])
        digest = str(content.extra.get("digest") or (content.body or "")[:100])
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
            fh.write(html)
            html_path = fh.name

        # 自此 publish 子进程可能产生服务端草稿写副作用。
        rc, out, err = await run_easel_script(
            ["publish", "--html", html_path, "--cover", content.images[0],
             "--title", content.title, "--digest", digest],
            timeout=settings.wechat_mp_api_timeout_seconds,
        )
        data = _parse_json(out)
        media_id = (data or {}).get("media_id")
        if rc == 0 and data and data.get("success") and media_id:
            return PublishResult(
                success=False,
                effect_applied=True,
                retryable=False,
                platform_post_id=str(media_id),
                error="公众号草稿已保存（Easel 会话），尚未群发；请后台人工确认",
                raw_response={"stage": "draft_only", "write_started": True},
            )
        # 写进程已启动但未确认草稿 id：禁止 fallback/自动重试，转人工核验。
        return PublishResult(
            success=False,
            effect_applied=False,
            retryable=False,
            outcome_uncertain=True,
            error="Easel 草稿写入后状态无法确认，请到公众号后台核验",
            raw_response={"stage": "draft_save_unknown", "write_started": True, "stderr": err[:200]},
        )