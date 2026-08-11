"""Feature-gated adapter for ``porjo/youtubeuploader`` v1.25.5.

Unlike browser uploaders, this tool uses the official YouTube Data API and can
write the created ``youtube.Video`` resource to ``-metaJSONout``.  The receipt's
video ID—not exit code or human stdout—is the success boundary.

OAuth client secrets and token caches remain external disk state.  They are
never passed as values (only paths), copied to raw_response, or inherited from
the control-plane environment.  Each ai-ops account has an isolated token file.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from ..config import settings
from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from ..runtime.receipts import write_publish_receipt
from .base import PublisherBase
from .subprocess_utils import communicate_bounded, stop_process_group


_AUDITED_VERSION = "v1.25.5"
_VERSION_RE = re.compile(r"Youtubeuploader\s+version:\s*(v?[0-9]+(?:\.[0-9]+){2})", re.I)
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
_ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
_ENV_ALLOWLIST = {
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "WINDIR",
}


@dataclass(slots=True)
class _CommandResult:
    started: bool
    returncode: int | None = None
    timed_out: bool = False
    error: str | None = None


class YoutubeUploaderPublisher(PublisherBase):
    platform = Platform.YOUTUBE
    kind = PublisherKind.YOUTUBE_UPLOADER

    def __init__(self) -> None:
        self.binary = settings.youtube_uploader_bin
        self.timeout_seconds = settings.youtube_uploader_timeout_seconds
        self.last_login_error: str | None = None

    def _journal_result(
        self,
        content: PublishContent,
        result: PublishResult,
    ) -> PublishResult:
        """Persist the redacted parsed result before returning to the worker."""
        write_publish_receipt(
            job_id=content.job_id,
            operation_id=content.operation_id,
            publisher_kind=self.kind.value,
            result=result,
        )
        return result

    def _account_home(self, account_id: int, *, create: bool) -> Path:
        if account_id <= 0:
            raise ValueError("account_id 必须是正整数")
        root = settings.youtube_uploader_profile_root.expanduser().resolve()
        home = root / f"account_{account_id}"
        if home.exists() and home.is_symlink():
            raise ValueError("YouTube CLI 账号目录不能是符号链接")
        if create:
            home.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                home.chmod(0o700)
            except OSError:
                pass
        return home

    def _token_file(self, account_id: int) -> Path:
        return self._account_home(account_id, create=False) / "request.token"

    def _secrets_file(self, account_id: int) -> Path:
        return self._account_home(account_id, create=False) / "client_secrets.json"

    def _subprocess_env(self, account_id: int) -> dict[str, str]:
        home = self._account_home(account_id, create=True)
        env = {key: value for key, value in os.environ.items() if key in _ENV_ALLOWLIST}
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "NO_COLOR": "1",
                "TERM": "dumb",
            }
        )
        return env

    def _resolved_binary(self) -> str | None:
        return shutil.which(self.binary)

    async def _stop_process(self, proc: asyncio.subprocess.Process) -> None:
        await stop_process_group(proc)

    async def _run(
        self,
        account_id: int,
        *args: str,
        timeout: int | None = None,
    ) -> _CommandResult:
        binary = self._resolved_binary()
        if binary is None:
            return _CommandResult(started=False, error="youtubeuploader 未安装或不可执行")
        try:
            env = self._subprocess_env(account_id)
            proc = await asyncio.create_subprocess_exec(
                binary,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=env["HOME"],
                env=env,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return _CommandResult(started=False, error=f"无法启动 youtubeuploader: {exc}")

        try:
            await asyncio.wait_for(
                communicate_bounded(proc),
                timeout=float(timeout or self.timeout_seconds),
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
            # The upload process did start.  A local pipe/process-management
            # failure must therefore remain an unknown write, never a normal
            # preflight failure that permits fallback.
            try:
                await self._stop_process(proc)
            except Exception:
                pass
            return _CommandResult(
                started=True,
                returncode=proc.returncode,
                error=f"youtubeuploader 执行状态无法确认: {type(exc).__name__}",
            )
        return _CommandResult(started=True, returncode=proc.returncode)

    async def _audited_version_ready(self, account_id: int) -> tuple[bool, str]:
        binary = self._resolved_binary()
        if binary is None:
            return False, "youtubeuploader 未安装或不可执行"
        proc: asyncio.subprocess.Process | None = None
        try:
            env = self._subprocess_env(account_id)
            proc = await asyncio.create_subprocess_exec(
                binary,
                "-version",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=env["HOME"],
                env=env,
                start_new_session=True,
            )
            stdout, _ = await asyncio.wait_for(communicate_bounded(proc), timeout=15)
        except asyncio.CancelledError:
            if proc is not None:
                try:
                    await self._stop_process(proc)
                except Exception:
                    pass
            raise
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return False, f"无法启动 youtubeuploader: {exc}"
        except TimeoutError:
            if proc is not None:
                try:
                    await self._stop_process(proc)
                except Exception:
                    pass
            return False, "youtubeuploader -version 超时"
        assert proc is not None
        if proc.returncode != 0:
            return False, f"youtubeuploader -version 失败（退出码 {proc.returncode}）"
        match = _VERSION_RE.search(stdout.decode("utf-8", "replace"))
        version = match.group(1) if match else ""
        normalized = version if version.startswith("v") else f"v{version}" if version else ""
        if normalized != _AUDITED_VERSION:
            return False, (
                f"youtubeuploader 版本 {version or 'unknown'} 未经审计，"
                f"仅允许 {_AUDITED_VERSION}"
            )
        return True, normalized

    def _auth_ready(self, account_id: int) -> tuple[bool, str, Path | None, Path | None]:
        secrets = self._secrets_file(account_id)
        token = self._token_file(account_id)
        for label, path in (("client_secrets", secrets), ("token", token)):
            if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
                return False, f"YouTube {label} 文件不存在或不是受控普通文件", None, None
            try:
                if path.stat().st_size > 1024 * 1024:
                    return False, f"YouTube {label} 文件异常过大", None, None
                path.chmod(0o600)
                if path.stat().st_mode & 0o077:
                    return False, f"YouTube {label} 文件权限必须是 0600", None, None
                if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
                    return False, f"YouTube {label} 文件 owner 不属于当前进程", None, None
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return False, f"YouTube {label} 文件不可读或不是合法 JSON", None, None
            if not isinstance(data, dict):
                return False, f"YouTube {label} 文件结构无效", None, None
            if label == "client_secrets":
                section = data.get("installed") or data.get("web")
                if not isinstance(section, dict) or not section.get("client_id"):
                    return False, "YouTube client_secrets 缺少 OAuth client_id", None, None
            elif not (data.get("refresh_token") or data.get("access_token")):
                return False, "YouTube token 缺少 OAuth token", None, None
        return True, "", secrets.resolve(), token.resolve()

    def _validate_video(self, content: PublishContent) -> tuple[Path | None, str | None]:
        if content.content_type != ContentType.VIDEO or len(content.videos) != 1:
            return None, "youtubeuploader 当前要求恰好一个本地视频文件"
        candidate = Path(content.videos[0]).expanduser()
        if candidate.is_symlink():
            return None, "YouTube 视频不能是符号链接"
        try:
            video = candidate.resolve(strict=True)
            root = settings.youtube_uploader_asset_root.expanduser().resolve()
        except OSError:
            return None, "YouTube 视频不存在或不可读"
        mime, _ = mimetypes.guess_type(video.name)
        if (
            not video.is_file()
            or not video.is_relative_to(root)
            or video.suffix.lower() not in _ALLOWED_VIDEO_SUFFIXES
            or not (mime or "").startswith("video/")
        ):
            return None, f"YouTube 视频必须是 {root} 内的受支持本地视频"
        size = video.stat().st_size
        if size <= 0 or size > settings.youtube_uploader_max_video_bytes:
            return None, "YouTube 视频为空或超过配置大小上限"
        return video, None

    def _metadata(self, content: PublishContent) -> tuple[dict | None, str | None]:
        extra = content.extra or {}
        privacy = str(extra.get("youtube_privacy", "private"))
        if privacy not in {"private", "unlisted", "public"}:
            return None, "youtube_privacy 只能是 private/unlisted/public"
        if not content.title.strip() or len(content.title) > 100:
            return None, "YouTube 标题不能为空且不能超过 100 字符"
        if len(content.body) > 5000:
            return None, "YouTube description 不能超过 5000 字符"
        category_id = str(extra.get("youtube_category_id", ""))
        if category_id and not category_id.isdigit():
            return None, "youtube_category_id 必须是数字"
        language = str(extra.get("youtube_language", "zh-Hans"))
        if not re.fullmatch(r"[A-Za-z0-9-]{2,16}", language):
            return None, "youtube_language 格式无效"
        tags = [str(tag) for tag in content.tags]
        if any(len(tag) > 100 for tag in tags):
            return None, "YouTube 单个 tag 不能超过 100 字符"
        metadata = {
            "title": content.title.strip(),
            "description": content.body,
            "tags": tags,
            "privacyStatus": privacy,
            "language": language,
            "madeForKids": bool(extra.get("youtube_made_for_kids", False)),
            "containsSyntheticMedia": bool(
                extra.get("youtube_contains_synthetic_media", False)
            ),
        }
        if category_id:
            metadata["categoryId"] = category_id
        return metadata, None

    @staticmethod
    def _read_receipt(path: Path) -> tuple[str, str, dict] | None:
        try:
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size > 2 * 1024 * 1024
            ):
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        video_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(video_id, str) or not _VIDEO_ID_RE.fullmatch(video_id):
            return None
        return video_id, f"https://www.youtube.com/watch?v={video_id}", data

    @staticmethod
    def _write_recovery_evidence(
        home: Path,
        attempt_id: str,
        receipt: tuple[str, str, dict] | None,
        *,
        outcome: str,
        job_id: int | None = None,
    ) -> None:
        """Best-effort redacted breadcrumb; this method must never mask write state."""
        try:
            recovery = home / "recovery"
            recovery.mkdir(mode=0o700, exist_ok=True)
            payload: dict[str, object] = {
                "attempt_id": attempt_id,
                "reconciliation_tag": f"aiops_{attempt_id[:12]}",
                "outcome": outcome,
            }
            if job_id is not None and job_id > 0:
                payload["job_id"] = job_id
            if receipt:
                video_id, video_url, data = receipt
                status = data.get("status") if isinstance(data, dict) else None
                payload.update(
                    {
                        "video_id": video_id,
                        "video_url": video_url,
                        "privacy": status.get("privacyStatus")
                        if isinstance(status, dict)
                        else None,
                    }
                )
            path = recovery / f"{attempt_id}.json"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
        except OSError:
            pass

    async def login(self, account_id: int, credential: dict) -> bool:
        """Validate pre-provisioned OAuth files; upstream has no auth-only command."""
        self.last_login_error = None
        ready, reason = await self._audited_version_ready(account_id)
        if not ready:
            self.last_login_error = reason
            return False
        ready, reason, _, _ = self._auth_ready(account_id)
        if not ready:
            self.last_login_error = (
                f"{reason}；请按 docs/cli-adapters.md 在可信终端预置 OAuth token"
            )
            return False
        return True

    async def publish(
        self,
        account_id: int,
        credential: dict,
        content: PublishContent,
    ) -> PublishResult:
        ready, reason = await self._audited_version_ready(account_id)
        if not ready:
            return PublishResult(success=False, error=reason)
        auth_ready, reason, secrets, token = self._auth_ready(account_id)
        if not auth_ready or secrets is None or token is None:
            return PublishResult(success=False, error=reason)
        video, video_error = self._validate_video(content)
        if video is None:
            return PublishResult(success=False, error=video_error)
        metadata, metadata_error = self._metadata(content)
        if metadata is None:
            return PublishResult(success=False, error=metadata_error)

        home = self._account_home(account_id, create=True)
        runs = home / "runs"
        runs.mkdir(mode=0o700, exist_ok=True)
        attempt_id = (
            content.operation_id
            if content.operation_id and re.fullmatch(r"[a-f0-9]{32}", content.operation_id)
            else uuid.uuid4().hex
        )
        metadata["tags"] = [*metadata["tags"], f"aiops_{attempt_id[:12]}"]
        with tempfile.TemporaryDirectory(prefix=f"upload-{attempt_id[:12]}-", dir=runs) as temp_name:
            temp = Path(temp_name)
            meta_path = temp / "metadata.json"
            receipt_path = temp / "receipt.json"
            fd = os.open(meta_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(metadata, handle, ensure_ascii=False, separators=(",", ":"))
            receipt_fd = os.open(
                receipt_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            os.close(receipt_fd)

            argv = (
                f"-filename={video}",
                f"-metaJSON={meta_path}",
                f"-metaJSONout={receipt_path}",
                f"-secrets={secrets}",
                f"-cache={token}",
                "-oAuthPort=-1",
                "-quiet=true",
                "-notify=false",
                "-sendFilename=false",
                "-chunksize=16777216",
            )
            try:
                result = await self._run(account_id, *argv)
            except asyncio.CancelledError:
                receipt = self._read_receipt(receipt_path)
                self._write_recovery_evidence(
                    home,
                    attempt_id,
                    receipt,
                    outcome="confirmed_after_cancel" if receipt else "unknown_after_cancel",
                    job_id=content.job_id,
                )
                raise
            receipt = self._read_receipt(receipt_path)
            if receipt is not None:
                _, _, receipt_data = receipt
                status = receipt_data.get("status")
                actual_privacy = (
                    status.get("privacyStatus") if isinstance(status, dict) else None
                )
                if actual_privacy != metadata["privacyStatus"]:
                    evidence_outcome = "privacy_mismatch"
                elif result.returncode == 0 and not result.timed_out:
                    evidence_outcome = "confirmed"
                else:
                    evidence_outcome = "confirmed_partial"
                # Persist the parsed ID before TemporaryDirectory removes the
                # upstream JSON receipt and before control returns to the DB
                # finalization layer.
                self._write_recovery_evidence(
                    home,
                    attempt_id,
                    receipt,
                    outcome=evidence_outcome,
                    job_id=content.job_id,
                )
            elif result.started:
                self._write_recovery_evidence(
                    home,
                    attempt_id,
                    None,
                    outcome="unknown_after_upload",
                    job_id=content.job_id,
                )

        raw = {
            "adapter": "youtubeuploader",
            "adapter_version": _AUDITED_VERSION,
            "action": "video-upload",
            "write_started": result.started,
            "exit_code": result.returncode,
            "privacy": metadata["privacyStatus"],
            "attempt_id": attempt_id,
        }
        if receipt is not None:
            video_id, video_url, receipt_data = receipt
            status = receipt_data.get("status")
            actual_privacy = (
                status.get("privacyStatus") if isinstance(status, dict) else None
            )
            privacy_matches = actual_privacy == metadata["privacyStatus"]
            if not privacy_matches:
                raw.update(
                    {
                        "outcome": "published_partial",
                        "actual_privacy": actual_privacy,
                        "needs_reconciliation": True,
                    }
                )
                return self._journal_result(
                    content,
                    PublishResult(
                        success=False,
                        effect_applied=True,
                        retryable=False,
                        platform_post_id=video_id,
                        platform_url=video_url,
                        error="YouTube 视频已创建，但实际 privacy 与请求不一致；请人工核验",
                        raw_response=raw,
                    ),
                )
            raw["outcome"] = (
                "confirmed" if result.returncode == 0 and not result.timed_out
                else "published_partial"
            )
            if raw["outcome"] == "published_partial":
                raw["needs_reconciliation"] = True
            return self._journal_result(
                content,
                PublishResult(
                    success=True,
                    effect_applied=True,
                    platform_post_id=video_id,
                    platform_url=video_url,
                    raw_response=raw,
                ),
            )
        raw["outcome"] = "unknown" if result.started else "not_started"
        return self._journal_result(
            content,
            PublishResult(
                success=False,
                outcome_uncertain=result.started,
                error=(
                    "YouTube 上传结果未知；请先到 YouTube Studio 核验，再决定是否手动重发"
                    if result.started
                    else (result.error or "youtubeuploader 未启动")
                ),
                raw_response=raw,
            ),
        )

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        ready, _ = await self._audited_version_ready(account_id)
        if not ready:
            return AccountHealth.UNKNOWN
        auth_ready, _, _, _ = self._auth_ready(account_id)
        # Upstream exposes no auth-only/read-only health command.  A structurally
        # valid disk token is useful but not proof that Google will accept it.
        return AccountHealth.DEGRADED if auth_ready else AccountHealth.EXPIRED
