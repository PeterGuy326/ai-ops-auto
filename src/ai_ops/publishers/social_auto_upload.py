"""dreammis/social-auto-upload 集成 wrapper（校正版，基于上游真实代码）。

上游真实入口：
  - CLI:  `python sau_cli.py <platform> <action> --account <name> --file <path> --title ...`
          platform = douyin / xiaohongshu / kuaishou / bilibili
          action   = login / check / upload_video / upload_note
  - HTTP: Flask, 默认端口 5409
          POST /postVideo  字段：fileList, accountList, type(1=xhs 2=tencent 3=douyin 4=ks),
                                  title, tags, category, enableTimer, thumbnail, isDraft 等

⚠️ 不要在这里写平台逻辑——所有签名/反爬/上传都在上游 uploader/ 目录。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from ..config import settings
from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from ..runtime.browser_engine import build_subprocess_env
from .base import PublisherBase


# 平台名（CLI 子命令）
SAU_PLATFORM_MAP: dict[Platform, str] = {
    Platform.DOUYIN: "douyin",
    Platform.XIAOHONGSHU: "xiaohongshu",
    Platform.BILIBILI: "bilibili",
    Platform.KUAISHOU: "kuaishou",
    Platform.WECHAT_VIDEO: "tencent",  # CLI 暂未覆盖，仅 HTTP 用
    Platform.TIKTOK: "tiktok",
    Platform.YOUTUBE: "youtube",
}

# HTTP /postVideo 用的 type 编号（仅 4 个平台支持 HTTP）
SAU_HTTP_TYPE_MAP: dict[Platform, int] = {
    Platform.XIAOHONGSHU: 1,
    Platform.WECHAT_VIDEO: 2,
    Platform.DOUYIN: 3,
    Platform.KUAISHOU: 4,
}


class SocialAutoUploadPublisher(PublisherBase):
    kind = PublisherKind.SOCIAL_AUTO_UPLOAD

    def __init__(self, platform: Platform):
        if platform not in SAU_PLATFORM_MAP:
            raise ValueError(f"social-auto-upload 不支持 {platform}")
        self.platform = platform
        self.sau_platform = SAU_PLATFORM_MAP[platform]

    async def login(self, account_id: int, credential: dict) -> bool:
        """触发 SAU 的登录流程。

        SAU 用 account_name 作为索引（不是 cookie 文件路径），cookie 由 SAU 内部管理。
        我们把 account_id 直接当 account_name 用（"acc_{id}"），第一次扫码登录后
        cookie 落到 SAU 的 cookiesFile/<platform>/acc_{id}.json，由我们镜像加密。
        """
        cmd = [
            sys.executable, "sau_cli.py",
            self.sau_platform, "login",
            "--account", f"acc_{account_id}",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(settings.external_sau_path),
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
        # credential 此处不直接落盘——SAU 用 account_name 索引，cookie 落地由 login() 完成
        # 如果业务侧维护了独立 cookie 池，这里把它写回 SAU 的 cookiesFile（兜底）
        self._sync_cookie_if_needed(account_id, credential)

        if settings.external_sau_url:
            return await self._publish_via_http(account_id, content)
        return await self._publish_via_cli(account_id, content)

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        """调 SAU 的 check 子命令判活。"""
        cmd = [
            sys.executable, "sau_cli.py",
            self.sau_platform, "check",
            "--account", f"acc_{account_id}",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(settings.external_sau_path),
                env=build_subprocess_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            return AccountHealth.HEALTHY if proc.returncode == 0 else AccountHealth.EXPIRED
        except Exception:
            return AccountHealth.UNKNOWN

    # ---------------- 内部 ----------------

    def _sync_cookie_if_needed(self, account_id: int, credential: dict) -> None:
        """把业务侧加密存储的 cookie 同步回 SAU 的 cookiesFile（兜底）。

        SAU 的 cookie 路径约定参考其 conf.py BASE_DIR/cookiesFile/<platform>/<account>.json。
        TODO: 上线时校准实际路径名（可能是 cookies/ 或 cookiesFile/）。
        """
        if not credential:
            return
        cookie_dir = settings.external_sau_path / "cookiesFile" / self.sau_platform
        cookie_dir.mkdir(parents=True, exist_ok=True)
        path = cookie_dir / f"acc_{account_id}.json"
        path.write_text(json.dumps(credential, ensure_ascii=False), encoding="utf-8")

    async def _publish_via_cli(self, account_id: int, content: PublishContent) -> PublishResult:
        """subprocess 模式：调上游 sau_cli.py。

        子命令按 content_type 选择 upload_video / upload_note。
        """
        if not settings.external_sau_path.exists():
            return PublishResult(success=False, error=f"SAU 路径不存在: {settings.external_sau_path}")

        is_video = bool(content.videos) or content.content_type == ContentType.VIDEO
        action = "upload_video" if is_video else "upload_note"

        cmd = [
            sys.executable, "sau_cli.py",
            self.sau_platform, action,
            "--account", f"acc_{account_id}",
            "--title", content.title,
        ]
        if is_video:
            if not content.videos:
                return PublishResult(success=False, error="video 内容缺视频文件")
            cmd += ["--file", content.videos[0], "--desc", content.body or ""]
        else:
            if not content.images:
                return PublishResult(success=False, error="note 内容缺图片")
            cmd += ["--images", *content.images, "--note", content.body or ""]

        if content.tags:
            cmd += ["--tags", ",".join(content.tags)]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(settings.external_sau_path),
            env=build_subprocess_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        success = proc.returncode == 0
        return PublishResult(
            success=success,
            error=None if success else stderr.decode("utf-8", "ignore")[:1000],
            raw_response={"stdout": stdout.decode("utf-8", "ignore")[:2000], "cmd": " ".join(cmd)},
        )

    async def _publish_via_http(self, account_id: int, content: PublishContent) -> PublishResult:
        """HTTP 模式：调 SAU Flask 后端 POST /postVideo。

        ⚠️ 上游 /postVideo 不返回 platform_post_id 或 url（只返回任务受理），
        如果需要这些，得另外 fetch 数据采集。
        """
        import httpx

        if self.platform not in SAU_HTTP_TYPE_MAP:
            return PublishResult(
                success=False,
                error=f"SAU HTTP 模式不支持 {self.platform}，请用 CLI",
            )

        payload = {
            "fileList": content.videos or content.images,
            "accountList": [f"acc_{account_id}"],
            "type": SAU_HTTP_TYPE_MAP[self.platform],
            "title": content.title,
            "tags": content.tags,
            "category": 0,
            "enableTimer": False,
            "thumbnail": content.extra.get("thumbnail", ""),
            "isDraft": False,
            "videosPerDay": 1,
            "dailyTimes": [9],
            "startDays": 0,
        }
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(f"{settings.external_sau_url}/postVideo", json=payload)
        ok = resp.status_code == 200
        data = resp.json() if ok else {}
        success = ok and data.get("code") == 200
        return PublishResult(
            success=success,
            platform_post_id=None,  # SAU /postVideo 不返回 id
            platform_url=None,
            error=None if success else f"HTTP {resp.status_code}: {resp.text[:500]}",
            raw_response=data,
        )
