"""Feature-gated adapter for ``timerring/bilitool`` 0.1.3 (PyPI wheel).

bilitool is a third-party MIT CLI around Bilibili's member web endpoints.  It
uses undocumented consumer APIs, not the Bilibili Open Platform.  The adapter
is therefore a default-off canary with the same receipt discipline as the
other CLI adapters.

Audited upstream contract (``bilitool==0.1.3``, wheel sha256
``acafca670e4e12a636ccaa98cdd72b8895252aba15a62ac6fc395a658712effd``,
identical to git tag ``v0.1.3``):

* ``bilitool --version`` prints ``bilitool 0.1.3 and source code at ...``.
* ``bilitool upload <video> --copyright N --title T --desc D --tid N
  --tag a,b --source S --cdn LINE`` publishes a new archive.  Metadata goes
  through argv; there is no JSON mode, result file or log file.
* The created archive is announced only by a ``logging.info`` line on
  **stderr**: ``upload success!\\tbvid:BV...``.  Nothing on stdout identifies
  it.
* Exit codes are not a success signal.  ``cli()`` discards the boolean
  returned by ``publish_video``, so a publish the server rejects still exits
  ``0`` after one ``[ERROR]`` log line.  Only the bvid line is the success
  boundary.
* The ``append`` (分P) subcommand is deliberately not wired: it calls two
  ``http://`` member endpoints with ``verify=False`` while sending
  ``access_key``.  Auto-driving it would knowingly downgrade transport
  security for a credential.
* bilitool 0.1.3 is the last release exposing ``--copyright``; ``main``
  removed it, which makes every upload a repost.  The adapter pins 0.1.3 so
  original content can be declared.

Credential isolation is the adapter's job, not upstream's.  bilitool keeps
cookies in ``<site-packages>/bilitool/model/config.json`` inside its own
package directory and offers no data-path flag or environment hook.  One
shared install would mean one shared account for every ai-ops account.  Each
account therefore gets a dedicated virtualenv under
``BILITOOL_PROFILE_ROOT/account_<id>/venv``; the adapter runs that venv's
``bilitool`` and never the ambient ``PATH`` copy.  Cookies never enter argv,
``raw_response`` or the Fernet-encrypted database credential.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import mimetypes
import os
from pathlib import Path
import re

from ..config import settings
from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from ..runtime.receipts import write_publish_receipt
from ..runtime.subprocess import communicate_bounded, stop_process_group
from .base import AgentContractRendererUnavailable, PublisherBase


_AUDITED_VERSION = "0.1.3"
_VERSION_RE = re.compile(r"^bilitool\s+([0-9]+(?:\.[0-9]+){2})\s+and source code at")
# Upstream logs this through logging.info() -> stderr, with a literal TAB.
_SUCCESS_RE = re.compile(r"upload success![ \t]+bvid:(BV[0-9A-Za-z]{10})")
_BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")
_CHECK_ACCOUNT_RE = re.compile(r"^Current account:\s*(.+?)\s*$", re.MULTILINE)
_CHECK_NO_LOGIN = "There is currently no login account"
_CHECK_FAILED = "Check failed, please check the info"
_ALLOWED_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".flv", ".avi", ".wmv", ".m4v"}
_CDN_LINES = ("qn", "bldsa", "ws", "bda2", "tx")
_MAX_TITLE_CHARS = 80
_MAX_DESC_CHARS = 2000
_MAX_DYNAMIC_CHARS = 2000
_MAX_TAG_CHARS = 20
_MAX_TAGS = 10
_AGENT_EXTRA_KEYS = (
    "bilibili_copyright",
    "bilibili_tid",
    "bilibili_cdn_line",
    "bilibili_dynamic",
)
# bilitool's own argparse default tid (138). The adapter always passes every
# flag explicitly because omitted flags silently inherit upstream defaults,
# and `main` HEAD additionally carries the previous upload's values over in
# config.json.
_DEFAULT_TID = 138
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
# tqdm renders transfer progress on stderr. A large upload emits far more than
# the shared 64 KiB stderr window and the bvid line is the *last* thing
# printed, so the default limit would turn a real success into an unknown
# outcome. The tail is what matters, so retain far more and slice below.
_STDERR_LIMIT = 8 * 1024 * 1024


@dataclass(slots=True)
class _CommandResult:
    started: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None


def _clean_output(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", "replace")
    else:
        text = value or ""
    # tqdm rewrites one line with carriage returns; keep each final state.
    return text.replace("\r", "\n")


def _validated_metadata(content: PublishContent) -> dict[str, object]:
    """Validate and normalise the metadata bilitool will submit."""

    if content.content_type != ContentType.VIDEO or len(content.videos) != 1:
        raise ValueError("B站 CLI 当前要求恰好一个本地视频文件")
    if content.images:
        raise ValueError("B站 CLI 不支持图片资产")
    if "\x00" in content.title or "\x00" in content.body:
        raise ValueError("B站标题或正文含 NUL 字符")

    extra = content.extra or {}
    unknown = set(extra).difference(_AGENT_EXTRA_KEYS)
    if unknown:
        raise ValueError(f"B站 CLI 不支持的 extra 字段: {sorted(unknown)}")

    title = content.title.strip()
    if not title:
        raise ValueError("B站标题不能为空")
    if len(title) > _MAX_TITLE_CHARS:
        raise ValueError(f"B站标题不能超过 {_MAX_TITLE_CHARS} 字")
    desc = content.body.strip()
    if len(desc) > _MAX_DESC_CHARS:
        raise ValueError(f"B站简介不能超过 {_MAX_DESC_CHARS} 字")

    tags = [str(tag).strip() for tag in content.tags]
    if any(not tag for tag in tags):
        raise ValueError("B站标签不能为空字符串")
    if any(len(tag) > _MAX_TAG_CHARS for tag in tags):
        raise ValueError(f"B站单个标签不能超过 {_MAX_TAG_CHARS} 字")
    if len(tags) > _MAX_TAGS:
        raise ValueError(f"B站最多 {_MAX_TAGS} 个标签")

    copyright_value = extra.get("bilibili_copyright", 2)
    if isinstance(copyright_value, bool) or copyright_value not in (1, 2):
        raise ValueError("bilibili_copyright 只能是 1（自制）或 2（转载）")
    tid_value = extra.get("bilibili_tid", _DEFAULT_TID)
    if isinstance(tid_value, bool) or not isinstance(tid_value, int) or tid_value <= 0:
        raise ValueError("bilibili_tid 必须是正整数分区 ID")
    cdn_line = extra.get("bilibili_cdn_line", "")
    if not isinstance(cdn_line, str) or (cdn_line and cdn_line not in _CDN_LINES):
        raise ValueError(
            "bilibili_cdn_line 只能是 " + "/".join(_CDN_LINES) + " 或留空自动探测"
        )
    dynamic = extra.get("bilibili_dynamic", "")
    if not isinstance(dynamic, str) or len(dynamic) > _MAX_DYNAMIC_CHARS:
        raise ValueError(f"bilibili_dynamic 必须是不超过 {_MAX_DYNAMIC_CHARS} 字的字符串")

    return {
        "copyright": copyright_value,
        "title": title,
        "desc": desc,
        "tid": tid_value,
        "tag": ",".join(tags),
        # Upstream sends `source` only for copyright == 2.
        "source": "来源于网络",
        "cdn": cdn_line,
        "dynamic": dynamic,
    }


def project_bilibili_agent_payload(content: PublishContent) -> dict[str, object]:
    """Return the exact bilitool upload metadata contract without host paths."""

    try:
        metadata = _validated_metadata(content)
    except ValueError as exc:
        raise AgentContractRendererUnavailable(str(exc)) from None
    return metadata


def _parse_bvid(stderr: str) -> str | None:
    """Return the bvid only when upstream's own success line is present."""

    match = _SUCCESS_RE.search(stderr)
    return match.group(1) if match else None


def _parse_login_account(stdout: str) -> str | None:
    match = _CHECK_ACCOUNT_RE.search(stdout)
    return match.group(1) if match else None


class BilibiliToolPublisher(PublisherBase):
    """bilitool 0.1.3 upload adapter, gated behind a feature flag."""

    platform = Platform.BILIBILI
    kind = PublisherKind.BILITOOL
    # Paused for Agent exact execution: bilitool exposes no audited read-only
    # probe binding this account's venv to the intended Bilibili identity
    # (``check`` prints a mutable nickname, not a stable id).
    agent_contract_renderer_descriptor = None

    def __init__(self) -> None:
        self.binary = settings.bilitool_bin
        self.timeout_seconds = settings.bilitool_timeout_seconds
        self.last_login_error: str | None = None

    def _journal_result(self, content: PublishContent, result: PublishResult) -> PublishResult:
        """Persist a redacted write receipt before returning to the worker."""

        write_publish_receipt(
            job_id=content.job_id,
            operation_id=content.operation_id,
            publisher_kind=self.kind.value,
            result=result,
        )
        return result

    # ---------------- account isolation ----------------

    def _account_home(self, account_id: int, *, create: bool) -> Path:
        """Return ``<root>/account_<id>``, refusing symlinked paths."""

        if account_id <= 0:
            raise ValueError("account_id 必须是正整数")
        root = settings.bilitool_profile_root.expanduser().resolve()
        home = root / f"account_{account_id}"
        if home.is_symlink():
            raise ValueError("B站 CLI 账号目录不能是符号链接")
        if create:
            home.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                home.chmod(0o700)
            except OSError:
                pass
        return home

    def _venv_dir(self, account_id: int) -> Path:
        venv = self._account_home(account_id, create=False) / "venv"
        if venv.is_symlink():
            raise ValueError("B站 CLI venv 不能是符号链接")
        return venv

    def _resolved_binary(self, account_id: int) -> str | None:
        """Only the account's own venv binary; never the ambient PATH copy."""

        try:
            venv = self._venv_dir(account_id)
        except (OSError, ValueError):
            return None
        binary = venv / ("Scripts" if os.name == "nt" else "bin") / Path(self.binary).name
        if not binary.is_file() or binary.is_symlink() or not os.access(binary, os.X_OK):
            return None
        return str(binary)

    def _cookie_file(self, account_id: int) -> Path:
        return self._account_home(account_id, create=False) / "cookie.json"

    def _subprocess_env(self, account_id: int, *, create: bool) -> dict[str, str]:
        home = self._account_home(account_id, create=create)
        env = {key: value for key, value in os.environ.items() if key in _ENV_ALLOWLIST}
        env.update(
            {
                "HOME": str(home),
                "USERPROFILE": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "NO_COLOR": "1",
                "TERM": "dumb",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        return env

    # ---------------- subprocess ----------------

    async def _stop_process(self, proc: asyncio.subprocess.Process) -> None:
        await stop_process_group(proc)

    async def _run(
        self,
        account_id: int,
        *args: str,
        timeout: int | None = None,
    ) -> _CommandResult:
        binary = self._resolved_binary(account_id)
        if binary is None:
            return _CommandResult(started=False, error="bilitool 未安装或不可执行")
        try:
            env = self._subprocess_env(account_id, create=True)
        except (OSError, ValueError) as exc:
            return _CommandResult(started=False, error=f"B站 CLI 账号目录不可用: {exc}")
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                *args,
                # Never a TTY: upstream's QR login blocks on input() and then
                # polls forever, so an interactive login must not start here.
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=env["HOME"],
                env=env,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return _CommandResult(started=False, error=f"无法启动 bilitool: {exc}")

        try:
            stdout, stderr = await asyncio.wait_for(
                communicate_bounded(proc, stderr_limit=_STDERR_LIMIT),
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
            return _CommandResult(started=True, timed_out=True, error="bilitool 执行超时")
        except Exception as exc:
            # The upload may already have reached Bilibili. A local pipe or
            # process failure after spawn is an unknown write and must not
            # enable fallback or retry.
            try:
                await self._stop_process(proc)
            except Exception:
                pass
            return _CommandResult(
                started=True,
                returncode=proc.returncode,
                error=f"bilitool 执行状态无法确认: {type(exc).__name__}",
            )

        return _CommandResult(
            started=True,
            returncode=proc.returncode,
            stdout=_clean_output(stdout),
            stderr=_clean_output(stderr),
        )

    async def _audited_version_ready(self, account_id: int) -> tuple[bool, str]:
        result = await self._run(account_id, "--version", timeout=15)
        if not result.started:
            return False, result.error or "bilitool 不可用"
        if result.returncode != 0:
            return False, f"bilitool --version 失败（退出码 {result.returncode}）"
        match = _VERSION_RE.match(result.stdout.strip())
        version = match.group(1) if match else ""
        if version != _AUDITED_VERSION:
            return (
                False,
                f"bilitool 版本 {version or 'unknown'} 未经审计，仅允许 {_AUDITED_VERSION}",
            )
        return True, version

    # ---------------- session ----------------

    async def _session_health(self, account_id: int) -> tuple[AccountHealth, str]:
        """Probe the stored login through bilitool's read-only ``check``.

        Upstream exits ``0`` for the logged-out and failed cases too, so the
        decision rests on its printed marker, not on the return code.
        """

        ready, reason = await self._audited_version_ready(account_id)
        if not ready:
            return AccountHealth.UNKNOWN, reason
        result = await self._run(account_id, "check", timeout=60)
        if not result.started:
            return AccountHealth.UNKNOWN, result.error or "bilitool 不可用"
        if result.timed_out:
            return AccountHealth.UNKNOWN, "bilitool check 超时"
        if _parse_login_account(result.stdout) is not None:
            return AccountHealth.HEALTHY, ""
        if _CHECK_NO_LOGIN in result.stdout:
            return AccountHealth.EXPIRED, "B站 CLI 登录态缺失或已失效"
        if _CHECK_FAILED in result.stdout:
            return AccountHealth.UNKNOWN, "B站 CLI 无法在线验证登录态"
        # Traceback, argparse error or unexpected output: not proof of logout.
        return AccountHealth.UNKNOWN, "bilitool check 没有返回可识别的登录状态"

    async def login(self, account_id: int, credential: dict) -> bool:
        """Import an operator-provided cookie file; never start a QR login.

        The operator runs ``bilitool login --export`` in a trusted terminal,
        which writes ``cookie.json`` to the working directory, and places it
        at ``<BILITOOL_PROFILE_ROOT>/account_<id>/cookie.json`` (mode 0600).
        This call only imports that file into the account's isolated venv.
        """

        self.last_login_error = None
        ready, reason = await self._audited_version_ready(account_id)
        if not ready:
            self.last_login_error = reason
            return False
        cookie_file = self._cookie_file(account_id)
        if cookie_file.is_symlink() or cookie_file.parent.is_symlink():
            self.last_login_error = "B站 cookie 文件不能是符号链接"
            return False
        if not cookie_file.is_file():
            self.last_login_error = (
                "隔离 venv 尚未导入登录态；请在可信终端执行 bilitool login --export，"
                f"并把 cookie.json 放到 {cookie_file.parent}"
            )
            return False
        try:
            cookie_file.chmod(0o600)
            payload = json.loads(cookie_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            self.last_login_error = "B站 cookie 文件不可读或不是合法 JSON"
            return False
        if not _is_exported_login_payload(payload):
            self.last_login_error = "B站 cookie 文件结构无效（需为 bilitool --export 的原始输出）"
            return False

        # Re-importing is idempotent upstream: it overwrites the venv's stored
        # cookies and then verifies them online.
        result = await self._run(account_id, "login", "-f", str(cookie_file), timeout=60)
        if not result.started:
            self.last_login_error = result.error or "bilitool 未启动"
            return False
        if result.timed_out:
            self.last_login_error = "bilitool login 超时"
            return False
        if "Login success!" not in result.stdout:
            self.last_login_error = "B站 cookie 已失效，请重新扫码导出"
            return False
        health, reason = await self._session_health(account_id)
        self.last_login_error = None if health == AccountHealth.HEALTHY else reason
        return health == AccountHealth.HEALTHY

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        health, _ = await self._session_health(account_id)
        return health

    def _validate_video(self, content: PublishContent) -> tuple[Path | None, str | None]:
        candidate = Path(content.videos[0]).expanduser()
        if candidate.is_symlink():
            return None, "B站视频不能是符号链接"
        try:
            video = candidate.resolve(strict=True)
            configured_root = (
                settings.agent_asset_vault_root
                if content.exact_approval
                else settings.bilitool_asset_root
            )
            root = configured_root.expanduser().resolve()
        except OSError:
            return None, "B站视频不存在或不可读"
        mime, _ = mimetypes.guess_type(video.name)
        if (
            not video.is_file()
            or not video.is_relative_to(root)
            or video.suffix.lower() not in _ALLOWED_VIDEO_SUFFIXES
            or not (mime or "").startswith("video/")
        ):
            return None, f"B站视频必须是 {root} 内的受支持本地视频"
        size = video.stat().st_size
        if size <= 0 or size > settings.bilitool_max_video_bytes:
            return None, "B站视频为空或超过配置大小上限"
        return video, None

    # ---------------- PublisherBase ----------------

    async def publish(
        self,
        account_id: int,
        credential: dict,
        content: PublishContent,
    ) -> PublishResult:
        if content.exact_approval:
            try:
                self.render_agent_contract_payload(content)
            except AgentContractRendererUnavailable as exc:
                return PublishResult(success=False, retryable=False, error=str(exc))

        try:
            metadata = _validated_metadata(content)
        except ValueError as exc:
            return PublishResult(success=False, retryable=False, error=str(exc))

        # Environment problems (binary missing, venv not provisioned for this
        # account, session logged out) are not a verdict on the content: leave
        # ``retryable`` at its default so the registry may fall back to the
        # next adapter and the job keeps its retry budget.
        ready, reason = await self._audited_version_ready(account_id)
        if not ready:
            return PublishResult(success=False, error=reason)
        video, video_error = self._validate_video(content)
        if video is None:
            return PublishResult(success=False, retryable=False, error=video_error)
        health, reason = await self._session_health(account_id)
        if health != AccountHealth.HEALTHY:
            return PublishResult(success=False, error=reason)

        argv = [
            "upload",
            str(video),
            "--copyright",
            str(metadata["copyright"]),
            "--title",
            str(metadata["title"]),
            "--desc",
            str(metadata["desc"]),
            "--tid",
            str(metadata["tid"]),
            "--tag",
            str(metadata["tag"]),
            "--source",
            str(metadata["source"]),
            "--dynamic",
            str(metadata["dynamic"]),
            "--cdn",
            str(metadata["cdn"]),
        ]
        result = await self._run(account_id, *argv)
        raw: dict[str, object] = {
            "adapter": "bilitool",
            "adapter_version": _AUDITED_VERSION,
            "action": "video-upload",
            "write_started": result.started,
            "exit_code": result.returncode,
        }
        if not result.started:
            # Nothing reached Bilibili; the registry may fall back.
            raw["outcome"] = "not_started"
            return PublishResult(
                success=False,
                retryable=False,
                effect_applied=False,
                outcome_uncertain=False,
                error=result.error or "bilitool 未启动",
                raw_response=raw,
            )

        bvid = _parse_bvid(result.stderr)
        if bvid is not None:
            # The receipt is the success boundary, even when the process was
            # later killed by the timeout or exited non-zero: the archive
            # exists, and re-running would create a duplicate submission.
            raw["outcome"] = "confirmed" if result.returncode == 0 else "confirmed_partial"
            if raw["outcome"] == "confirmed_partial":
                raw["needs_reconciliation"] = True
            return self._journal_result(
                content,
                PublishResult(
                    success=True,
                    effect_applied=True,
                    platform_post_id=bvid,
                    platform_url=f"https://www.bilibili.com/video/{bvid}",
                    raw_response=raw,
                ),
            )

        # Started but no bvid line. The bytes may be sitting in Bilibili's
        # upload bin, the publish may have been rejected (upstream still exits
        # 0), or the transfer may have died: the adapter cannot tell these
        # apart, so this stays unknown and blocks fallback and retry.
        raw["outcome"] = "unknown"
        return self._journal_result(
            content,
            PublishResult(
                success=False,
                retryable=False,
                outcome_uncertain=True,
                error=(
                    "bilitool 上传结果未知（未收到 bvid 回执）；"
                    "请先到 B站创作中心稿件箱核验，再决定是否手动重发"
                ),
                raw_response=raw,
            ),
        )


def _is_exported_login_payload(payload: object) -> bool:
    """Recognise the shape ``bilitool login --export`` writes to cookie.json."""

    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("access_token"):
        return False
    cookie_info = data.get("cookie_info")
    cookies = cookie_info.get("cookies") if isinstance(cookie_info, dict) else None
    # Upstream indexes positions 0..4 without checking; refuse to hand it a
    # file that would crash with IndexError instead of importing.
    return isinstance(cookies, list) and len(cookies) >= 5
