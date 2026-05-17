"""white0dew/XiaohongshuSkills 集成 wrapper（小红书专项加固，2.7k⭐）。

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
import sys

from ..config import settings
from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from ..runtime.browser_engine import build_subprocess_env
from .base import PublisherBase


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
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=build_subprocess_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0

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

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=build_subprocess_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        ok = proc.returncode == 0
        return PublishResult(
            success=ok,
            error=None if ok else stderr.decode("utf-8", "ignore")[:1000],
            raw_response={
                "stdout": stdout.decode("utf-8", "ignore")[:2000],
                "cmd": " ".join(cmd),
            },
        )

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        # TODO: XHS Skills 有 list-feeds 子命令可探活
        return AccountHealth.UNKNOWN
