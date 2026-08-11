"""dreammis/social-auto-upload 集成 wrapper（校正版，基于上游真实代码）。

上游真实入口：
  - CLI:  `python sau_cli.py <platform> <action> --account <name> --file <path> --title ...`
          platform = douyin / xiaohongshu / kuaishou / bilibili
          action   = login / check / upload-video / upload-note
  - HTTP: Flask, 默认端口 5409
          POST /postVideo  字段：fileList, accountList, type(1=xhs 2=tencent 3=douyin 4=ks),
                                  title, tags, category, enableTimer, thumbnail, isDraft 等

⚠️ 不要在这里写平台逻辑——所有签名/反爬/上传都在上游 uploader/ 目录。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import sys
from pathlib import Path
import tempfile

from ..config import settings
from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from ..runtime.browser_engine import build_subprocess_env
from .base import PublisherBase
from .subprocess_utils import communicate_bounded, stop_process_group


# 平台名（CLI 子命令）
SAU_CLI_PLATFORM_MAP: dict[Platform, str] = {
    Platform.DOUYIN: "douyin",
    Platform.XIAOHONGSHU: "xiaohongshu",
    Platform.BILIBILI: "bilibili",
    Platform.KUAISHOU: "kuaishou",
}

# HTTP /postVideo 用的 type 编号（仅 4 个平台支持 HTTP）
SAU_HTTP_TYPE_MAP: dict[Platform, int] = {
    Platform.XIAOHONGSHU: 1,
    Platform.WECHAT_VIDEO: 2,
    Platform.DOUYIN: 3,
    Platform.KUAISHOU: 4,
}

# Registry coverage is the union of the audited CLI and HTTP contracts. Do not
# add platform names merely because an upstream module exists: the public CLI
# parser is the executable contract.
SAU_PLATFORM_MAP: dict[Platform, str] = {
    **SAU_CLI_PLATFORM_MAP,
    Platform.WECHAT_VIDEO: "tencent",
}


# 登录成功但上游只把登录态留在自己的磁盘目录时，仍需给 ai-ops 一个非空、
# 可加密落库的凭证引用。它不是 cookie，publish() 绝不能把它写进上游 cookie 文件。
SAU_CREDENTIAL_REF_KEY = "_ai_ops_credential_ref"
SAU_CREDENTIAL_REF_PROVIDER = "social_auto_upload"


@dataclass(slots=True)
class _CommandResult:
    started: bool
    returncode: int | None = None
    timed_out: bool = False
    error: str | None = None


class SocialAutoUploadPublisher(PublisherBase):
    kind = PublisherKind.SOCIAL_AUTO_UPLOAD

    def __init__(self, platform: Platform):
        if platform not in SAU_PLATFORM_MAP:
            raise ValueError(f"social-auto-upload 不支持 {platform}")
        self.platform = platform
        self.sau_platform = SAU_PLATFORM_MAP[platform]
        # PublisherBase 的 login() 契约只能返回 bool；保留一条不含凭证内容的错误，
        # 供 API/CLI 在需要时给出可操作的失败原因。
        self.last_login_error: str | None = None

    async def _stop_process(self, proc: asyncio.subprocess.Process) -> None:
        await stop_process_group(proc)

    async def _run_cli(self, cmd: list[str], *, timeout_seconds: int) -> _CommandResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(settings.external_sau_path),
                env=build_subprocess_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except (FileNotFoundError, PermissionError, OSError):
            return _CommandResult(started=False, error="SAU 命令无法启动")
        try:
            # Consume output to avoid a blocked child, but never persist it:
            # upstream browser logs may contain cookies or private content.
            await asyncio.wait_for(
                communicate_bounded(proc), timeout=float(timeout_seconds)
            )
        except asyncio.CancelledError:
            try:
                await self._stop_process(proc)
            except Exception:
                pass
            raise
        except TimeoutError:
            try:
                await self._stop_process(proc)
            except Exception:
                pass
            return _CommandResult(started=True, returncode=proc.returncode, timed_out=True)
        except Exception as exc:
            try:
                await self._stop_process(proc)
            except Exception:
                pass
            return _CommandResult(
                started=True,
                returncode=proc.returncode,
                error=f"SAU 执行状态无法确认: {type(exc).__name__}",
            )
        return _CommandResult(started=True, returncode=proc.returncode)

    async def login(self, account_id: int, credential: dict) -> bool:
        """触发 SAU 的登录流程。

        SAU 用 account_name 作为索引（不是 cookie 文件路径），cookie 由 SAU 内部管理。
        我们把 account_id 直接当 account_name 用（"acc_{id}"），第一次扫码登录后
        从当前 cookies/<platform>_<account>.json 或兼容的旧目录读取并镜像加密。
        """
        self.last_login_error = None
        if self.platform not in SAU_CLI_PLATFORM_MAP:
            self.last_login_error = "该平台没有经过审计的 SAU CLI 登录命令"
            return False
        cmd = [
            sys.executable,
            "sau_cli.py",
            self.sau_platform,
            "login",
            "--account",
            f"acc_{account_id}",
        ]
        result = await self._run_cli(
            cmd,
            timeout_seconds=min(settings.sau_cli_timeout_seconds, 300),
        )
        if not result.started:
            self.last_login_error = "无法启动或执行 SAU 登录命令"
            return False
        if result.timed_out:
            self.last_login_error = "SAU 登录命令超时"
            return False
        if result.returncode != 0:
            self.last_login_error = f"SAU 登录命令失败（退出码 {result.returncode}）"
            return False

        cookie_path = self._find_cookie_file(account_id)
        if cookie_path is None:
            # 某些 SAU 版本自行管理 profile/cookie，命令成功并不一定暴露 JSON。
            # 记录引用即可让 worker 继续按 account_name 使用上游磁盘态。
            credential.clear()
            credential.update(self._credential_reference(account_id))
            return True

        try:
            cookie_path.chmod(0o600)
            cookie = json.loads(cookie_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self.last_login_error = "SAU 登录成功，但生成的 cookie 文件不是合法 JSON"
            return False

        if not isinstance(cookie, dict) or not cookie:
            self.last_login_error = "SAU 登录成功，但生成的 cookie 不是非空 JSON 对象"
            return False

        # 原地更新，api_login_account 持有的正是这个 dict，随后会 Fernet 加密落库。
        credential.clear()
        credential.update(cookie)
        return True

    async def publish(
        self,
        account_id: int,
        credential: dict,
        content: PublishContent,
    ) -> PublishResult:
        # credential 此处不直接落盘——SAU 用 account_name 索引，cookie 落地由 login() 完成
        # 如果业务侧维护了独立 cookie 池，这里把它写回 SAU 的 cookiesFile（兜底）
        self._sync_cookie_if_needed(account_id, credential)

        if settings.external_sau_url and self.platform in SAU_HTTP_TYPE_MAP:
            return await self._publish_via_http(account_id, content)
        if self.platform in SAU_CLI_PLATFORM_MAP:
            return await self._publish_via_cli(account_id, content)
        return PublishResult(
            success=False,
            retryable=False,
            effect_applied=False,
            error=f"SAU 没有可执行的 {self.platform.value} 适配器",
            raw_response={"adapter": "social-auto-upload", "write_started": False},
        )

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        """调 SAU 的 check 子命令判活。"""
        if self.platform not in SAU_CLI_PLATFORM_MAP:
            return AccountHealth.UNKNOWN
        cmd = [
            sys.executable,
            "sau_cli.py",
            self.sau_platform,
            "check",
            "--account",
            f"acc_{account_id}",
        ]
        try:
            result = await self._run_cli(
                cmd,
                timeout_seconds=min(settings.sau_cli_timeout_seconds, 60),
            )
            if not result.started or result.timed_out:
                return AccountHealth.UNKNOWN
            return (
                AccountHealth.HEALTHY
                if result.returncode == 0
                else AccountHealth.EXPIRED
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return AccountHealth.UNKNOWN

    # ---------------- 内部 ----------------

    def _sync_cookie_if_needed(self, account_id: int, credential: dict) -> None:
        """把业务侧加密存储的 cookie 同步回 SAU 自己的 cookie 文件（兜底）。"""
        if not credential or self._is_credential_reference(credential):
            return

        # 优先覆盖上游已在使用的路径；全新安装采用当前 SAU 的 cookies/
        # <platform>_<account>.json 约定。这样既兼容旧 cookiesFile 布局，也不会
        # 同时制造两份互相漂移的登录态。
        path = self._find_cookie_file(account_id) or self._cookie_candidates(account_id)[0]
        root = settings.external_sau_path.expanduser().resolve()
        parent = path.parent.expanduser().resolve(strict=False)
        if not parent.is_relative_to(root) or path.is_symlink():
            raise ValueError("SAU cookie 路径不安全")
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            parent.chmod(0o700)
        except OSError:
            pass
        payload = json.dumps(credential, ensure_ascii=False, separators=(",", ":"))
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            path.chmod(0o600)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _cookie_candidates(self, account_id: int) -> tuple[Path, ...]:
        """返回当前与历史 SAU 常见的 account cookie 路径，按优先级排序。"""
        root = settings.external_sau_path
        account_name = f"acc_{account_id}"
        filename = f"{self.sau_platform}_{account_name}.json"
        return (
            # 当前 sau_cli.py 的 resolve_account_file() 布局。
            root / "cookies" / filename,
            # 兼容曾使用同名文件、但目录叫 cookiesFile 的版本。
            root / "cookiesFile" / filename,
            # 兼容旧 wrapper 与部分 fork 的平台子目录布局。
            root / "cookies" / self.sau_platform / f"{account_name}.json",
            root / "cookiesFile" / self.sau_platform / f"{account_name}.json",
            # 少数 fork 只按 account_name 分文件。
            root / "cookies" / f"{account_name}.json",
            root / "cookiesFile" / f"{account_name}.json",
        )

    def _find_cookie_file(self, account_id: int) -> Path | None:
        return next(
            (
                path
                for path in self._cookie_candidates(account_id)
                if path.is_file() and not path.is_symlink() and not path.parent.is_symlink()
            ),
            None,
        )

    def _credential_reference(self, account_id: int) -> dict:
        return {
            SAU_CREDENTIAL_REF_KEY: {
                "provider": SAU_CREDENTIAL_REF_PROVIDER,
                "platform": self.sau_platform,
                "account_name": f"acc_{account_id}",
            }
        }

    @staticmethod
    def _is_credential_reference(credential: dict) -> bool:
        reference = credential.get(SAU_CREDENTIAL_REF_KEY)
        return (
            isinstance(reference, dict) and reference.get("provider") == SAU_CREDENTIAL_REF_PROVIDER
        )

    async def _publish_via_cli(self, account_id: int, content: PublishContent) -> PublishResult:
        """subprocess 模式：调上游 sau_cli.py。

        子命令按 content_type 选择 upload-video / upload-note。
        """
        if not settings.external_sau_path.exists():
            return PublishResult(
                success=False, error=f"SAU 路径不存在: {settings.external_sau_path}"
            )

        is_video = bool(content.videos) or content.content_type == ContentType.VIDEO
        action = "upload-video" if is_video else "upload-note"

        cmd = [
            sys.executable,
            "sau_cli.py",
            self.sau_platform,
            action,
            "--account",
            f"acc_{account_id}",
            "--title",
            content.title,
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

        result = await self._run_cli(
            cmd,
            timeout_seconds=settings.sau_cli_timeout_seconds,
        )
        if not result.started:
            return PublishResult(
                success=False,
                error=result.error or "SAU 发布命令未启动",
                raw_response={"adapter": "social-auto-upload", "write_started": False},
            )
        # Upstream exposes neither a structured post identity nor reliable
        # read-after-write.  rc=0 is process completion, not publication proof.
        return PublishResult(
            success=False,
            retryable=False,
            outcome_uncertain=True,
            error="SAU 写入已启动但无 post ID/URL 回执；请先到平台核验",
            raw_response={
                "adapter": "social-auto-upload",
                "action": action,
                "write_started": True,
                "exit_code": result.returncode,
                "timed_out": result.timed_out,
                "outcome": "unknown",
            },
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
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(f"{settings.external_sau_url}/postVideo", json=payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            return PublishResult(
                success=False,
                retryable=False,
                outcome_uncertain=True,
                error="SAU HTTP 请求结果无法确认；请先核验上游任务与平台",
                raw_response={
                    "adapter": "social-auto-upload-http",
                    "write_started": True,
                    "outcome": "unknown",
                },
            )
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            data = {}
        upstream_code = data.get("code") if isinstance(data, dict) else None
        return PublishResult(
            success=False,
            retryable=False,
            outcome_uncertain=True,
            error="SAU HTTP 只返回任务受理状态，无平台 post ID/URL；请人工对账",
            raw_response={
                "adapter": "social-auto-upload-http",
                "write_started": True,
                "http_status": resp.status_code,
                "upstream_code": upstream_code,
                "outcome": "unknown",
            },
        )
