"""white0dew/XiaohongshuSkills 集成 wrapper（小红书可选兼容路径）。

上游真实入口（校对自 scripts/publish_pipeline.py + SKILL.md）：
  python scripts/publish_pipeline.py
    --title "标题" --content "正文"
    [--images <local>... | --image-urls <url>... | --video <local> | --video-url <url>]
    [--account <name>] [--headless] [--auto-publish] [--preview]

约束：
  - 图文发布必须有图片
  - 视频发布必须有视频
  - 图片和视频不可混合（二选一）
  - 默认无头；未登录会切有窗口

启用时机：SAU 在小红书风控失败时 fallback；或需要小红书评论/检索/互动时主用。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import sys

from ..config import settings
from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from ..runtime.browser_engine import build_subprocess_env
from .base import PublisherBase
from .subprocess_utils import communicate_bounded, stop_process_group


@dataclass(slots=True)
class _CommandResult:
    started: bool
    returncode: int | None = None
    timed_out: bool = False


class XhsSkillsPublisher(PublisherBase):
    platform = Platform.XIAOHONGSHU
    kind = PublisherKind.XHS_TOOLKIT  # 复用枚举，实际指 XiaohongshuSkills

    @property
    def _skills_path(self):
        # XiaohongshuSkills 默认作为 submodule 拉到 external/XiaohongshuSkills
        return settings.external_sau_path.parent / "XiaohongshuSkills"

    @property
    def _publish_script(self):
        return self._skills_path / "scripts" / "publish_pipeline.py"

    async def _run_cli(self, cmd: list[str], *, timeout_seconds: int) -> _CommandResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self._skills_path),
                env=build_subprocess_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except (FileNotFoundError, PermissionError, OSError):
            return _CommandResult(started=False)
        try:
            await asyncio.wait_for(
                communicate_bounded(proc), timeout=float(timeout_seconds)
            )
        except asyncio.CancelledError:
            try:
                await stop_process_group(proc)
            except Exception:
                pass
            raise
        except TimeoutError:
            try:
                await stop_process_group(proc)
            except Exception:
                pass
            return _CommandResult(started=True, returncode=proc.returncode, timed_out=True)
        except Exception:
            try:
                await stop_process_group(proc)
            except Exception:
                pass
            return _CommandResult(started=True, returncode=proc.returncode)
        return _CommandResult(started=True, returncode=proc.returncode)

    async def login(self, account_id: int, credential: dict) -> bool:
        """XHS Skills 通过 --preview 启动有窗口浏览器扫码登录。"""
        if not self._publish_script.exists():
            return False
        cmd = [
            sys.executable, str(self._publish_script),
            "--account", f"acc_{account_id}",
            "--preview",  # 仅打开浏览器，不发布
            "--title", "login", "--content", "login",
            # 提供占位图，XHS Skills 要求有 media；TODO 后续看是否有 login-only 子命令
        ]
        result = await self._run_cli(
            cmd,
            timeout_seconds=min(settings.sau_cli_timeout_seconds, 300),
        )
        return result.started and not result.timed_out and result.returncode == 0

    async def publish(
        self,
        account_id: int,
        credential: dict,
        content: PublishContent,
    ) -> PublishResult:
        if not self._publish_script.exists():
            return PublishResult(
                success=False,
                error=f"XHS Skills 路径不存在: {self._publish_script}",
            )

        is_video = bool(content.videos) or content.content_type == ContentType.VIDEO

        # 强约束：图文要图、视频要视频，二选一
        if is_video and not content.videos:
            return PublishResult(success=False, error="视频笔记必须提供 video 文件或 URL")
        if not is_video and not content.images:
            return PublishResult(success=False, error="图文笔记必须提供至少一张图片")

        cmd = [
            sys.executable, str(self._publish_script),
            "--account", f"acc_{account_id}",
            "--title", content.title,
            "--content", content.body or "",
            "--auto-publish",
        ]
        # 风控对抗：高风控平台默认有窗口模式（更不易识别），可被 settings 覆盖
        if settings.browser_headless:
            cmd.append("--headless")
        if is_video:
            video = content.videos[0]
            cmd += ["--video-url", video] if video.startswith("http") else ["--video", video]
        else:
            url_imgs = [x for x in content.images if x.startswith("http")]
            local_imgs = [x for x in content.images if not x.startswith("http")]
            if url_imgs:
                cmd += ["--image-urls", *url_imgs]
            if local_imgs:
                cmd += ["--images", *local_imgs]

        result = await self._run_cli(
            cmd,
            timeout_seconds=settings.sau_cli_timeout_seconds,
        )
        if not result.started:
            return PublishResult(
                success=False,
                error="XHS Skills 发布命令未启动",
                raw_response={"adapter": "xiaohongshu-skills", "write_started": False},
            )
        # The upstream command has no structured note ID/URL contract.  Even a
        # zero exit code cannot be promoted to confirmed publication.
        return PublishResult(
            success=False,
            retryable=False,
            outcome_uncertain=True,
            error="XHS Skills 写入已启动但无 note ID/URL 回执；请先到平台核验",
            raw_response={
                "adapter": "xiaohongshu-skills",
                "write_started": True,
                "exit_code": result.returncode,
                "timed_out": result.timed_out,
                "outcome": "unknown",
            },
        )

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        # TODO: XHS Skills 有 list-feeds 子命令可探活
        return AccountHealth.UNKNOWN
