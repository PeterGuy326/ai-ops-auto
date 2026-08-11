"""Experimental adapter for ``BAIGUANGMEI/zhihu-cli`` 0.2.4.

The upstream CLI is intentionally kept as an external, pinned tool.  It uses
undocumented consumer endpoints and its write commands do not yet expose JSON,
stdin/content-file input, or an idempotency key.  This adapter therefore treats
every unconfirmed result *after the write subprocess starts* as an unknown
platform outcome.  The registry and worker must not fallback/retry that result.

Authentication is disk-backed.  Upstream hard-codes ``~/.zhihu-cli`` so each
ai-ops account gets an isolated synthetic HOME under
``ZHIHU_CLI_PROFILE_ROOT/account_<id>``.  Cookies are never passed in argv and
are never copied into logs/raw_response.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
import re
import secrets
import shutil
import warnings

from ..config import settings
from ..core.enums import AccountHealth, AssetType, ContentType, Platform, PublisherKind
from ..core.external_identity import (
    normalize_zhihu_external_account_id,
    zhihu_external_account_id_from_whoami as _external_account_id_from_whoami,
)
from ..core.schemas import PublishContent, PublishResult
from ..runtime.account_lease import AccountOperationLease, AccountOperationLeaseTimeout
from ..runtime.receipts import write_publish_receipt
from .base import (
    AgentContractAssetRule,
    AgentContractRendererDescriptor,
    AgentContractRendererUnavailable,
    PublisherBase,
)
from .subprocess_utils import communicate_bounded, stop_process_group


_AUDITED_VERSION = "0.2.4"
_VERSION_RE = re.compile(r"\bversion\s+([0-9]+(?:\.[0-9]+){2})\b", re.IGNORECASE)
_ARTICLE_SUCCESS_RE = re.compile(
    r"Article\s+published!\s+ID:\s*(\d+).*?"
    r"https://zhuanlan\.zhihu\.com/p/(\d+)",
    re.IGNORECASE | re.DOTALL,
)
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg"}
_ALLOWED_IMAGE_MIMES = {"image/jpeg"}
_MARKDOWN_RENDERER_VERSION = "3.10.3"
_ZHIHU_AGENT_EXTRA_KEYS = ("zhihu_topic_ids",)
_ZHIHU_MAX_TOPIC_IDS = 20
_ZHIHU_TOPIC_ID_RE = re.compile(r"^[1-9][0-9]{0,31}$")
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
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None


def _clean_output(value: bytes | str | None) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", "replace")
    else:
        text = value or ""
    return _ANSI_RE.sub("", text).replace("\r\n", "\n")


def _parse_article_confirmation(stdout: str) -> tuple[str, str] | None:
    """Return a confirmed ``(id, url)`` only when marker and URL agree."""
    match = _ARTICLE_SUCCESS_RE.search(_clean_output(stdout))
    if match is None or match.group(1) != match.group(2):
        return None
    article_id = match.group(1)
    return article_id, f"https://zhuanlan.zhihu.com/p/{article_id}"


def _parse_whoami_external_account_id(stdout: str) -> str | None:
    try:
        profile = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return _external_account_id_from_whoami(profile)


def project_zhihu_agent_payload(content: PublishContent) -> dict[str, object]:
    """Return the audited Zhihu CLI argv payload without filesystem paths."""

    if content.content_type not in {ContentType.IMAGE_TEXT, ContentType.LONG_ARTICLE}:
        raise AgentContractRendererUnavailable(
            "Zhihu Agent contract supports only image-text and long articles"
        )
    if content.videos:
        raise AgentContractRendererUnavailable("Zhihu Agent contract does not support video assets")
    if len(content.images) > 9:
        raise AgentContractRendererUnavailable(
            "Zhihu Agent contract supports at most 9 image assets"
        )
    if content.tags:
        raise AgentContractRendererUnavailable("Zhihu Agent contract does not support tags")
    if not content.title.strip() or not content.body.strip():
        raise AgentContractRendererUnavailable("Zhihu title and body must be non-empty")
    if "\x00" in content.title or "\x00" in content.body:
        raise AgentContractRendererUnavailable("Zhihu title and body must not contain NUL")

    extra = content.extra or {}
    if set(extra).difference(_ZHIHU_AGENT_EXTRA_KEYS):
        raise AgentContractRendererUnavailable(
            "Zhihu Agent contract contains unsupported extra fields"
        )
    raw_topic_ids = extra.get("zhihu_topic_ids", [])
    if not isinstance(raw_topic_ids, list):
        raise AgentContractRendererUnavailable("zhihu_topic_ids must be a list")
    if len(raw_topic_ids) > _ZHIHU_MAX_TOPIC_IDS:
        raise AgentContractRendererUnavailable(
            f"Zhihu Agent contract supports at most {_ZHIHU_MAX_TOPIC_IDS} topic IDs"
        )
    topic_ids: list[str] = []
    for value in raw_topic_ids:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise AgentContractRendererUnavailable("zhihu_topic_ids must contain numeric IDs")
        normalized = str(value)
        if _ZHIHU_TOPIC_ID_RE.fullmatch(normalized) is None:
            raise AgentContractRendererUnavailable(
                "zhihu_topic_ids must contain 1-32 digit positive IDs"
            )
        topic_ids.append(normalized)
    if len(set(topic_ids)) != len(topic_ids):
        raise AgentContractRendererUnavailable("zhihu_topic_ids must not contain duplicates")

    import markdown

    if markdown.__version__ != _MARKDOWN_RENDERER_VERSION:
        raise AgentContractRendererUnavailable(
            "Zhihu Agent contract Markdown renderer version is not audited"
        )
    body_html = markdown.markdown(
        content.body,
        extensions=["fenced_code", "tables", "nl2br"],
    )
    if len(body_html.encode("utf-8")) > settings.zhihu_cli_max_content_bytes:
        raise AgentContractRendererUnavailable(
            "Zhihu rendered body exceeds the configured CLI content-byte limit"
        )
    return {
        "action": "article",
        "topic_ids": topic_ids,
        "image_slots": [
            {"asset_type": AssetType.IMAGE.value, "index": index}
            for index in range(len(content.images))
        ],
        "title": content.title,
        "body_html": body_html,
    }


class ZhihuCliPublisher(PublisherBase):
    """CLI-first Zhihu article publisher, gated behind a feature flag."""

    platform = Platform.ZHIHU
    kind = PublisherKind.ZHIHU_CLI
    agent_contract_renderer_descriptor = AgentContractRendererDescriptor(
        renderer_id="zhihu-cli.article-argv",
        contract_version=(
            f"4+python-markdown-{_MARKDOWN_RENDERER_VERSION}"
            "+account-id+bounds-v1+media-preflight-v1"
        ),
        adapter_version=_AUDITED_VERSION,
        platform=platform,
        publisher_kind=kind,
        accepted_extra_keys=_ZHIHU_AGENT_EXTRA_KEYS,
        accepts_tags=False,
        requires_external_account_id=True,
        asset_rules=(
            AgentContractAssetRule(
                asset_type=AssetType.IMAGE,
                min_count=0,
                max_count=9,
            ),
        ),
    )

    def render_agent_contract_payload(self, content: PublishContent) -> dict[str, object]:
        return project_zhihu_agent_payload(content)

    def agent_contract_digest_material(self, content: PublishContent) -> dict[str, object]:
        """Bind only image assets that the audited CLI can actually upload."""

        images, _ = self._validate_images(content.images, exact_approval=True)
        if images is None:
            raise AgentContractRendererUnavailable(
                "Zhihu Agent contract image assets failed the audited media preflight"
            )
        return super().agent_contract_digest_material(content)

    def __init__(self) -> None:
        self.binary = settings.zhihu_cli_bin
        self.timeout_seconds = settings.zhihu_cli_timeout_seconds
        self.last_login_error: str | None = None
        self.last_external_account_id: str | None = None

    def _journal_result(
        self,
        content: PublishContent,
        result: PublishResult,
    ) -> PublishResult:
        """Persist a redacted write receipt before returning to the worker."""
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
        root = settings.zhihu_cli_profile_root.expanduser().resolve()
        home = root / f"account_{account_id}"
        if home.exists() and home.is_symlink():
            raise ValueError("知乎 CLI 账号目录不能是符号链接")
        if create:
            home.mkdir(mode=0o700, parents=True, exist_ok=True)
            profile = home / ".zhihu-cli"
            if profile.exists() and profile.is_symlink():
                raise ValueError("知乎 CLI profile 目录不能是符号链接")
            profile.mkdir(mode=0o700, exist_ok=True)
            try:
                home.chmod(0o700)
                profile.chmod(0o700)
            except OSError:
                pass
        return home

    def _cookie_file(self, account_id: int) -> Path:
        return self._account_home(account_id, create=False) / ".zhihu-cli" / "cookies.json"

    def _subprocess_env(self, account_id: int, *, create_profile: bool) -> dict[str, str]:
        home = self._account_home(account_id, create=create_profile)
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
            return _CommandResult(started=False, error="知乎 CLI 未安装或不可执行")
        try:
            env = self._subprocess_env(account_id, create_profile=True)
        except (OSError, ValueError) as exc:
            return _CommandResult(started=False, error=f"知乎 CLI profile 不可用: {exc}")

        try:
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
            return _CommandResult(started=False, error=f"无法启动知乎 CLI: {exc}")

        try:
            stdout, stderr = await asyncio.wait_for(
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
            return _CommandResult(started=True, timed_out=True, error="知乎 CLI 执行超时")
        except Exception as exc:
            # The article command may already have reached Zhihu.  Local pipe
            # failures after spawn are unknown writes and must not enable the
            # browser fallback.
            try:
                await self._stop_process(proc)
            except Exception:
                pass
            return _CommandResult(
                started=True,
                returncode=proc.returncode,
                error=f"知乎 CLI 执行状态无法确认: {type(exc).__name__}",
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
            return False, result.error or "知乎 CLI 不可用"
        if result.returncode != 0:
            return False, f"知乎 CLI --version 失败（退出码 {result.returncode}）"
        match = _VERSION_RE.search(result.stdout)
        version = match.group(1) if match else ""
        if version != _AUDITED_VERSION:
            return (
                False,
                f"知乎 CLI 版本 {version or 'unknown'} 未经审计，仅允许 {_AUDITED_VERSION}",
            )
        return True, version

    async def _session_identity(
        self,
        account_id: int,
    ) -> tuple[AccountHealth, str, str | None]:
        try:
            cookie_file = self._cookie_file(account_id)
        except (OSError, ValueError):
            return AccountHealth.UNKNOWN, "知乎 CLI profile 不可用", None
        if cookie_file.parent.is_symlink() or cookie_file.is_symlink():
            return AccountHealth.UNKNOWN, "知乎 CLI profile/cookie 不能是符号链接", None
        if not cookie_file.is_file():
            return AccountHealth.EXPIRED, "隔离 profile 尚未扫码登录", None
        try:
            cookie_file.chmod(0o600)
        except OSError:
            return AccountHealth.UNKNOWN, "无法收紧 cookies.json 权限", None

        result = await self._run(account_id, "whoami", "--json", timeout=30)
        if not result.started:
            return AccountHealth.UNKNOWN, result.error or "知乎 CLI 不可用", None
        if result.returncode != 0:
            combined = f"{result.stdout}\n{result.stderr}".lower()
            expired_markers = ("not authenticated", "session expired", "login")
            if any(marker in combined for marker in expired_markers):
                return AccountHealth.EXPIRED, "知乎 CLI 登录态已失效", None
            return AccountHealth.UNKNOWN, "知乎 CLI 无法在线验证登录态", None
        try:
            profile = json.loads(result.stdout)
        except json.JSONDecodeError:
            return AccountHealth.UNKNOWN, "知乎 CLI whoami 没有返回合法 JSON", None
        if isinstance(profile, dict) and (profile.get("id") or profile.get("url_token")):
            return AccountHealth.HEALTHY, "", _external_account_id_from_whoami(profile)
        return AccountHealth.UNKNOWN, "知乎 CLI whoami 返回缺少账号标识", None

    async def _session_health(self, account_id: int) -> tuple[AccountHealth, str]:
        health, reason, _ = await self._session_identity(account_id)
        return health, reason

    def _validate_images(
        self,
        images: list[str],
        *,
        exact_approval: bool = False,
    ) -> tuple[list[str] | None, str | None]:
        if len(images) > 9:
            return None, "知乎 CLI 单次最多允许 9 张图片"
        configured_root = (
            settings.agent_asset_vault_root if exact_approval else settings.zhihu_cli_asset_root
        )
        root = configured_root.expanduser().resolve()
        resolved: list[str] = []
        total_bytes = 0
        for raw in images:
            candidate = Path(raw).expanduser()
            if candidate.is_symlink():
                return None, "知乎 CLI 不接受符号链接图片"
            try:
                path = candidate.resolve(strict=True)
            except OSError:
                return None, "知乎 CLI 图片不存在或不可读"
            if not path.is_file() or not path.is_relative_to(root):
                return None, f"知乎 CLI 图片必须位于受控目录 {root}"
            mime, _ = mimetypes.guess_type(path.name)
            if (
                path.suffix.lower() not in _ALLOWED_IMAGE_SUFFIXES
                or mime not in _ALLOWED_IMAGE_MIMES
            ):
                return None, "知乎 CLI canary 仅接受 JPEG 图片"
            try:
                from PIL import Image

                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    with Image.open(path) as image:
                        if image.format != "JPEG":
                            return None, "知乎 CLI 图片扩展名与真实格式不一致"
                        image.verify()
            except Exception:
                return None, "知乎 CLI 图片不是可验证的 JPEG 文件"
            size = path.stat().st_size
            if size > settings.zhihu_cli_max_image_bytes:
                return None, "知乎 CLI 单张图片超过大小上限"
            total_bytes += size
            if total_bytes > settings.zhihu_cli_max_total_image_bytes:
                return None, "知乎 CLI 图片总大小超过上限"
            resolved.append(str(path))
        return resolved, None

    async def login(self, account_id: int, credential: dict) -> bool:
        """API login only verifies disk state; QR login must be an explicit TTY action."""
        self.last_login_error = None
        health, reason = await self._session_health(account_id)
        if health == AccountHealth.HEALTHY:
            return True
        self.last_login_error = f"{reason}；请在可信终端运行 ai-ops zhihu-login {account_id}"
        return False

    async def login_interactive(self, account_id: int) -> bool:
        """Run one lease-protected QR login; never accepts a cookie argument."""
        self.last_login_error = None
        self.last_external_account_id = None
        try:
            async with AccountOperationLease(
                account_id,
                timeout_seconds=settings.account_operation_lock_timeout_seconds,
            ):
                return await self._login_interactive_locked(account_id)
        except AccountOperationLeaseTimeout:
            self.last_login_error = "知乎账号正在执行其他操作，请稍后重试扫码登录"
            return False
        except OSError:
            self.last_login_error = "知乎账号操作锁不可用"
            return False

    async def _login_interactive_locked(self, account_id: int) -> bool:
        ready, reason = await self._audited_version_ready(account_id)
        if not ready:
            self.last_login_error = reason
            return False
        binary = self._resolved_binary()
        if binary is None:
            self.last_login_error = "知乎 CLI 未安装或不可执行"
            return False
        try:
            env = self._subprocess_env(account_id, create_profile=True)
        except (OSError, ValueError):
            self.last_login_error = "知乎 CLI profile 不可用"
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "login",
                "--qrcode",
                cwd=env["HOME"],
                env=env,
                start_new_session=True,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            self.last_login_error = f"无法启动知乎 CLI: {exc}"
            return False
        try:
            await asyncio.wait_for(proc.wait(), timeout=float(self.timeout_seconds))
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
            self.last_login_error = "知乎 CLI 扫码登录超时"
            return False
        finally:
            # Upstream keeps the QR PNG after login/logout.  It is only a
            # transport artifact, so remove it once the explicit login finishes.
            qr_path = Path(env["HOME"]) / ".zhihu-cli" / "login_qrcode.png"
            try:
                qr_path.unlink(missing_ok=True)
            except OSError:
                pass
        if proc.returncode != 0:
            self.last_login_error = f"知乎 CLI 扫码登录失败（退出码 {proc.returncode}）"
            return False
        health, reason, external_account_id = await self._session_identity(account_id)
        self.last_external_account_id = external_account_id
        self.last_login_error = None if health == AccountHealth.HEALTHY else reason
        return health == AccountHealth.HEALTHY

    async def publish(
        self,
        account_id: int,
        credential: dict,
        content: PublishContent,
    ) -> PublishResult:
        contract_payload: dict[str, object] | None = None
        if content.exact_approval:
            try:
                contract_payload = self.render_agent_contract_payload(content)
            except AgentContractRendererUnavailable as exc:
                return PublishResult(success=False, error=str(exc))
        else:
            if content.content_type in (ContentType.VIDEO, ContentType.AUDIO):
                return PublishResult(success=False, error="知乎 CLI 当前只接入专栏图文/长文")
            if not content.title.strip() or not content.body.strip():
                return PublishResult(success=False, error="知乎 CLI 文章标题和正文不能为空")
            if "\x00" in content.title or "\x00" in content.body:
                return PublishResult(success=False, error="知乎 CLI 标题或正文含 NUL 字符")

        ready, reason = await self._audited_version_ready(account_id)
        if not ready:
            return PublishResult(success=False, error=reason)
        if not content.exact_approval:
            health, reason = await self._session_health(account_id)
            if health != AccountHealth.HEALTHY:
                return PublishResult(success=False, error=reason)

        images, image_error = self._validate_images(
            content.images,
            exact_approval=content.exact_approval,
        )
        if images is None:
            return PublishResult(success=False, error=image_error)

        if contract_payload is not None:
            body = contract_payload["body_html"]
            topic_ids = contract_payload["topic_ids"]
            title = contract_payload["title"]
            assert isinstance(body, str)
            assert isinstance(topic_ids, list)
            assert isinstance(title, str)
        else:
            # Match the old browser adapter's Markdown rendering as closely as
            # the 0.2.4 contract allows. Upstream wraps this fragment in an extra
            # <p>, hence the integration remains canary-only until
            # --content-file/html is available upstream.
            import markdown

            body = markdown.markdown(
                content.body,
                extensions=["fenced_code", "tables", "nl2br"],
            )
            topic_ids = content.extra.get("zhihu_topic_ids", [])
            if not isinstance(topic_ids, list) or any(
                not str(value).isdigit() for value in topic_ids
            ):
                return PublishResult(success=False, error="zhihu_topic_ids 必须是数字 ID 列表")
            topic_ids = [str(value) for value in topic_ids]
            title = content.title.strip()
        if len(body.encode("utf-8")) > settings.zhihu_cli_max_content_bytes:
            return PublishResult(
                success=False,
                error="知乎 CLI 正文超过安全 argv 上限，改走浏览器 Publisher",
            )

        argv: list[str] = ["article"]
        for topic_id in topic_ids:
            argv.extend(["--topic", topic_id])
        for image in images:
            argv.extend(["--image", image])
        argv.extend(["--", title, body])

        if content.exact_approval:
            try:
                approved_external_account_id = normalize_zhihu_external_account_id(
                    content.approved_external_account_id
                )
            except ValueError:
                return PublishResult(
                    success=False,
                    retryable=False,
                    error="Agent contract 缺少已批准的知乎目标账号标识",
                )
            health, reason, observed_external_account_id = await self._session_identity(account_id)
            if health != AccountHealth.HEALTHY:
                return PublishResult(success=False, retryable=False, error=reason)
            if observed_external_account_id is None or not secrets.compare_digest(
                observed_external_account_id,
                approved_external_account_id,
            ):
                return PublishResult(
                    success=False,
                    retryable=False,
                    effect_applied=False,
                    outcome_uncertain=False,
                    error="知乎当前登录账号与批准目标不一致；写入未开始",
                )

        result = await self._run(account_id, *argv)
        raw = {
            "adapter": "zhihu-cli",
            "adapter_version": _AUDITED_VERSION,
            "action": "article",
            "write_started": result.started,
            "exit_code": result.returncode,
        }
        confirmed = _parse_article_confirmation(result.stdout)
        if result.returncode == 0 and confirmed is not None:
            article_id, article_url = confirmed
            raw["outcome"] = "confirmed"
            return self._journal_result(
                content,
                PublishResult(
                    success=True,
                    platform_post_id=article_id,
                    platform_url=article_url,
                    raw_response=raw,
                ),
            )

        # Once `article` starts, rc=0-without-id, rc!=0, timeout and malformed
        # output are all ambiguous: the server may already have accepted it.
        if confirmed is not None:
            # A matching success marker + numeric URL is valuable reconciliation
            # evidence even when the process later exits non-zero.  It is not
            # sufficient for SUCCESS under the audited 0.2.4 contract, but the
            # worker must persist it instead of discarding the only platform
            # identity available to the operator.
            raw["outcome"] = "unknown_with_candidate_identity"
        else:
            raw["outcome"] = "unknown" if result.started else "not_started"
        return self._journal_result(
            content,
            PublishResult(
                success=False,
                effect_applied=confirmed is not None,
                outcome_uncertain=result.started,
                platform_post_id=confirmed[0] if confirmed else None,
                platform_url=confirmed[1] if confirmed else None,
                error=(
                    "知乎 CLI 写入结果未知；请先到知乎核验，再决定是否手动重发"
                    if result.started
                    else (result.error or "知乎 CLI 未启动")
                ),
                raw_response=raw,
            ),
        )

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        ready, _ = await self._audited_version_ready(account_id)
        if not ready:
            return AccountHealth.UNKNOWN
        health, _ = await self._session_health(account_id)
        return health
