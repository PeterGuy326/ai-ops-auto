"""CLI-native GitHub Pages publisher for a locally managed Hexo repository.

The adapter deliberately exposes a narrow command surface: fixed Hexo argv,
explicit git remote/ref, and a verified remote commit.  Article data is never
interpolated into a shell command and command output is never persisted in a
``PublishResult``.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from PIL import Image

from ..config import settings
from ..core.enums import AccountHealth, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from .base import PublisherBase
from .subprocess_utils import communicate_bounded, stop_process_group


HEXO_FRONTMATTER_TEMPLATE = """---
title: {title}
date: {date}
tags:
{tags}
categories:
{categories}
{extra_fields}---

"""

_BUILD_TOOLS = {"pnpm", "npx"}
_REMOTE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")
_MAX_CAPTURE_BYTES = 256 * 1024
_PROCESS_STOP_GRACE_SECONDS = 3.0
_LOCK_POLL_SECONDS = 0.05
_ALLOWED_IMAGE_FORMATS = {
    ".gif": "GIF",
    ".jpeg": "JPEG",
    ".jpg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
}
_SUBPROCESS_ENV_ALLOWLIST = {
    "PATH",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "WINDIR",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    # SSH agent/path settings are transport configuration, not application
    # credentials.  Private key material itself is never copied into env.
    "SSH_AUTH_SOCK",
    "GIT_SSH",
    "GIT_SSH_COMMAND",
}


@dataclass(slots=True, frozen=True)
class _CommandResult:
    started: bool
    returncode: int | None = None
    stdout: str = ""
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.started and not self.timed_out and self.returncode == 0


@dataclass(slots=True, frozen=True)
class _ImagePlan:
    source: Path
    destination: Path
    site_path: str
    size: int
    device: int
    inode: int
    digest: str


@dataclass(slots=True, frozen=True)
class _GitPublishResult:
    success: bool
    commit_sha: str | None = None
    error: str | None = None
    retryable: bool = False
    outcome_uncertain: bool = False


@dataclass(slots=True)
class _RollbackPath:
    path: Path
    relative: str
    existed: bool
    original_content: bytes | None
    original_mode: int | None
    written_digest: str | None = None
    written_size: int | None = None


@dataclass(slots=True, frozen=True)
class _RollbackResult:
    success: bool
    error: str | None = None


class _RepositoryLockTimeout(TimeoutError):
    pass


class _RepositoryLock:
    """Kernel-backed lock shared by processes publishing the same repository."""

    def __init__(self, path: Path, timeout_seconds: float) -> None:
        self.path = path
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        self._fd: int | None = None
        self._locked = False

    def _open(self) -> int:
        if self.path.is_symlink():
            raise OSError("仓库锁文件不能是符号链接")
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("仓库锁必须是普通文件")
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise OSError("仓库锁文件 owner 不属于当前进程")
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                pass
            if os.name == "nt" and metadata.st_size == 0:
                os.write(fd, b"\0")
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _try_acquire(fd: int) -> bool:
        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False
        if os.name == "nt":
            import msvcrt

            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return True
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    return False
                raise
        raise OSError(f"当前操作系统不支持仓库锁: {os.name}")

    @staticmethod
    def _unlock(fd: int) -> None:
        if os.name == "posix":
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        elif os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    async def __aenter__(self) -> _RepositoryLock:
        self._fd = self._open()
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        try:
            while not self._try_acquire(self._fd):
                if asyncio.get_running_loop().time() >= deadline:
                    raise _RepositoryLockTimeout("等待博客仓库发布锁超时")
                await asyncio.sleep(_LOCK_POLL_SECONDS)
            self._locked = True
            return self
        except BaseException:
            os.close(self._fd)
            self._fd = None
            raise

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        if self._fd is None:
            return
        try:
            if self._locked:
                self._unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None
            self._locked = False


def _slugify(title: str) -> str:
    """Turn a mixed Chinese/ASCII title into a bounded filename-safe slug."""
    value = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", title)
    value = re.sub(r"-+", "-", value).strip("-").lower()
    if not value:
        value = "post-" + datetime.now().strftime("%Y%m%d%H%M%S")
    return value[:80]


def _yaml_scalar(value: object) -> str:
    """Emit a JSON scalar/container, which is also safe YAML syntax."""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def _yaml_list(items: list[str]) -> str:
    if not items:
        return "  []"
    return "\n".join(f"  - {_yaml_scalar(str(item))}" for item in items)


def _safe_commit_title(title: str) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", title)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:120] or "untitled"


def _valid_branch(branch: str) -> bool:
    if not _BRANCH_RE.fullmatch(branch):
        return False
    return not (
        ".." in branch
        or "//" in branch
        or "@{" in branch
        or branch.endswith(("/", ".", ".lock"))
        or any(part.startswith(".") or part.endswith(".lock") for part in branch.split("/"))
    )


class GitHubPagesPublisher(PublisherBase):
    """Publish Markdown through fixed Hexo and git CLI contracts."""

    platform = Platform.GITHUB_PAGES
    kind = PublisherKind.HEXO

    async def login(self, account_id: int, credential: dict) -> bool:
        """Probe configured remote/ref readability without mutating the remote."""
        del account_id, credential
        repo = settings.github_pages_path.expanduser().resolve()
        remote = settings.github_pages_remote
        branch = settings.github_pages_branch
        if not self._is_git_repo(repo) or not self._valid_git_target(remote, branch):
            return False
        if not await self._remote_exists(repo, remote):
            return False
        result = await self._run_argv(
            ["git", "ls-remote", "--exit-code", remote, f"refs/heads/{branch}"],
            cwd=repo,
            timeout_seconds=settings.github_pages_git_timeout_seconds,
        )
        return result.ok

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        return (
            AccountHealth.HEALTHY
            if await self.login(account_id, credential)
            else AccountHealth.EXPIRED
        )

    async def publish(
        self,
        account_id: int,
        credential: dict,
        content: PublishContent,
    ) -> PublishResult:
        del account_id, credential
        repo = settings.github_pages_path.expanduser().resolve()
        if not repo.is_dir():
            return self._failure("preflight", f"博客仓库不存在或不是目录: {repo}")
        if not self._is_git_repo(repo):
            return self._failure("preflight", f"{repo} 不是可用的 git 仓库")
        if settings.github_pages_engine != "hexo":
            return self._failure(
                "preflight",
                f"暂仅支持 hexo，当前 engine={settings.github_pages_engine}",
            )

        build_tool = settings.github_pages_build_tool
        if build_tool not in _BUILD_TOOLS:
            return self._failure("preflight", "Hexo build tool 仅允许 pnpm 或 npx")

        try:
            remote, branch = self._git_target()
            posts_relative, posts_dir = self._repo_subdirectory(
                repo, settings.github_pages_posts_dir, "文章目录"
            )
            images_relative, images_dir = self._repo_subdirectory(
                repo, settings.github_pages_images_dir, "图片目录"
            )
            categories = self._string_list(
                (content.extra or {}).get("categories", []), "categories"
            )
        except ValueError as exc:
            return self._failure("preflight", str(exc))

        slug = _slugify(content.title)
        post_path = posts_dir / f"{slug}.md"
        article_url = self._article_url(slug)
        post_relative = posts_relative / post_path.name

        if not settings.github_pages_dry_run and not self._valid_live_base_url(
            settings.github_pages_base_url
        ):
            return self._failure(
                "preflight",
                "live GitHub Pages 发布必须配置无凭证的 http(s) GITHUB_PAGES_BASE_URL",
            )

        if settings.github_pages_dry_run:
            if self._path_exists(post_path):
                return self._failure("preflight", "目标文章已存在，拒绝覆盖")
            try:
                image_plans, body = self._plan_images(
                    repo=repo,
                    images_relative=images_relative,
                    images_dir=images_dir,
                    slug=slug,
                    content=content,
                )
            except (OSError, ValueError) as exc:
                return self._failure("preflight", str(exc))
            rendered = self._render(content, categories, body)
            return PublishResult(
                success=True,
                effect_applied=False,
                retryable=False,
                platform_post_id=slug,
                platform_url=article_url,
                raw_response={
                    "dry_run": True,
                    "slug": slug,
                    "would_write_to": post_relative.as_posix(),
                    "rendered_bytes": len(rendered.encode("utf-8")),
                    "images_count": len(image_plans),
                },
            )

        lock_path = self._repository_lock_path(repo)
        try:
            async with _RepositoryLock(
                lock_path,
                settings.github_pages_lock_timeout_seconds,
            ):
                return await self._publish_live(
                    repo=repo,
                    posts_dir=posts_dir,
                    images_relative=images_relative,
                    images_dir=images_dir,
                    post_path=post_path,
                    post_relative=post_relative,
                    slug=slug,
                    article_url=article_url,
                    categories=categories,
                    content=content,
                    build_tool=build_tool,
                    remote=remote,
                    branch=branch,
                )
        except _RepositoryLockTimeout as exc:
            return self._failure("preflight", str(exc))
        except OSError as exc:
            return self._failure("preflight", f"博客仓库发布锁不可用: {type(exc).__name__}")

    async def _publish_live(
        self,
        *,
        repo: Path,
        posts_dir: Path,
        images_relative: Path,
        images_dir: Path,
        post_path: Path,
        post_relative: Path,
        slug: str,
        article_url: str,
        categories: list[str],
        content: PublishContent,
        build_tool: str,
        remote: str,
        branch: str,
    ) -> PublishResult:
        """Run every mutable live step while the repository lock is held."""
        if self._path_exists(post_path):
            return self._failure("preflight", "目标文章已存在，拒绝覆盖")
        try:
            image_plans, body = self._plan_images(
                repo=repo,
                images_relative=images_relative,
                images_dir=images_dir,
                slug=slug,
                content=content,
            )
        except (OSError, ValueError) as exc:
            return self._failure("preflight", str(exc))
        rendered = self._render(content, categories, body)

        if not await self._remote_exists(repo, remote):
            return self._failure("preflight", "配置的 git remote 不存在")

        clean = await self._run_argv(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repo,
            timeout_seconds=settings.github_pages_git_timeout_seconds,
        )
        if not clean.ok:
            return self._command_failure("preflight", "无法确认 git 工作区状态", clean)
        if clean.stdout.strip():
            return self._failure("preflight", "git 工作区不干净，拒绝自动发布")

        head = await self._run_argv(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repo,
            timeout_seconds=settings.github_pages_git_timeout_seconds,
        )
        preflight_head = self._verified_sha(head)
        if preflight_head is None:
            return self._command_failure("preflight", "无法确认发布前 HEAD", head)

        publish_paths = [post_path, *(plan.destination for plan in image_plans)]
        try:
            rollback_paths = self._snapshot_rollback_paths(repo, publish_paths)
        except (OSError, ValueError) as exc:
            return self._failure("preflight", f"无法建立发布回滚快照: {exc}")

        created_paths: list[Path] = []
        try:
            posts_dir.mkdir(parents=True, exist_ok=True)
            with post_path.open("x", encoding="utf-8") as handle:
                handle.write(rendered)
            created_paths.append(post_path)
            for plan in image_plans:
                plan.destination.parent.mkdir(parents=True, exist_ok=True)
                self._copy_planned_image(plan)
                created_paths.append(plan.destination)
        except (FileExistsError, OSError, ValueError) as exc:
            self._remove_created_paths(created_paths, repo)
            return self._failure("write", f"写入发布产物失败: {type(exc).__name__}")

        try:
            self._seal_rollback_paths(rollback_paths, repo)
        except (OSError, ValueError) as exc:
            self._remove_created_paths(created_paths, repo)
            return self._failure("write", f"无法确认本轮发布产物: {exc}")

        for action in ("clean", "generate"):
            result = await self._run_argv(
                [build_tool, "hexo", action],
                cwd=repo,
                timeout_seconds=settings.github_pages_build_timeout_seconds,
            )
            if not result.ok:
                failure = self._command_failure("build", f"hexo {action} 失败", result)
                return await self._rollback_or_report(
                    failure,
                    repo=repo,
                    preflight_head=preflight_head,
                    preflight_status=clean.stdout,
                    paths=rollback_paths,
                    unstage=False,
                )

        git_result = await self._git_publish(
            repo=repo,
            paths=publish_paths,
            slug=slug,
            title=content.title,
            remote=remote,
            branch=branch,
        )
        if not git_result.success:
            failure = PublishResult(
                success=False,
                effect_applied=False,
                retryable=git_result.retryable,
                outcome_uncertain=git_result.outcome_uncertain,
                error=git_result.error or "git 发布失败",
                raw_response={
                    "stage": "push" if git_result.commit_sha else "commit",
                    **({"commit_sha": git_result.commit_sha} if git_result.commit_sha else {}),
                },
            )
            if git_result.commit_sha is None and not git_result.outcome_uncertain:
                return await self._rollback_or_report(
                    failure,
                    repo=repo,
                    preflight_head=preflight_head,
                    preflight_status=clean.stdout,
                    paths=rollback_paths,
                    unstage=True,
                )
            return failure

        commit_sha = git_result.commit_sha
        return PublishResult(
            success=True,
            effect_applied=True,
            retryable=False,
            platform_post_id=commit_sha,
            platform_url=article_url,
            raw_response={
                "slug": slug,
                "commit_sha": commit_sha,
                "remote": remote,
                "branch": branch,
                "post_path": post_relative.as_posix(),
            },
        )

    @staticmethod
    def _is_git_repo(repo: Path) -> bool:
        git_marker = repo / ".git"
        return git_marker.exists() and not git_marker.is_symlink()

    @staticmethod
    def _valid_git_target(remote: str, branch: str) -> bool:
        return bool(_REMOTE_RE.fullmatch(remote)) and _valid_branch(branch)

    @classmethod
    def _git_target(cls) -> tuple[str, str]:
        remote = settings.github_pages_remote
        branch = settings.github_pages_branch
        if not cls._valid_git_target(remote, branch):
            raise ValueError("git remote 或 branch 格式不安全")
        return remote, branch

    async def _remote_exists(self, repo: Path, remote: str) -> bool:
        result = await self._run_argv(
            ["git", "remote"],
            cwd=repo,
            timeout_seconds=settings.github_pages_git_timeout_seconds,
        )
        return result.ok and remote in set(result.stdout.splitlines())

    @staticmethod
    def _repo_subdirectory(repo: Path, configured: str, label: str) -> tuple[Path, Path]:
        relative = Path(configured)
        if (
            not configured.strip()
            or relative.is_absolute()
            or relative == Path(".")
            or ".." in relative.parts
        ):
            raise ValueError(f"{label}必须是仓库内的非空相对路径")

        cursor = repo
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError(f"{label}不能经过符号链接")

        resolved_repo = repo.resolve()
        resolved = (resolved_repo / relative).resolve(strict=False)
        if not resolved.is_relative_to(resolved_repo):
            raise ValueError(f"{label}越出博客仓库")
        if resolved.exists() and not resolved.is_dir():
            raise ValueError(f"{label}不是目录")
        return relative, resolved

    @staticmethod
    def _path_exists(path: Path) -> bool:
        return path.exists() or path.is_symlink()

    @staticmethod
    def _repository_lock_path(repo: Path) -> Path:
        """Keep the persistent lock outside the worktree so git status stays clean."""
        resolved = repo.resolve()
        git_directory = resolved / ".git"
        if git_directory.is_dir() and not git_directory.is_symlink():
            return git_directory / "ai-ops-publish.lock"
        # Worktrees use a `.git` indirection file.  A sibling lock remains outside
        # the worktree without trusting or parsing that file here.
        return resolved.parent / f".{resolved.name}.ai-ops-publish.lock"

    @staticmethod
    def _string_list(value: object, field: str) -> list[str]:
        if value in (None, ""):
            return []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{field} 必须是字符串列表")
        return value

    def _plan_images(
        self,
        *,
        repo: Path,
        images_relative: Path,
        images_dir: Path,
        slug: str,
        content: PublishContent,
    ) -> tuple[list[_ImagePlan], str]:
        body = content.body or ""
        plans: list[_ImagePlan] = []
        destination_names: set[str] = set()
        image_root = images_dir / slug

        if image_root.is_symlink():
            raise ValueError("目标图片目录不能是符号链接")
        resolved_repo = repo.resolve()
        if not image_root.resolve(strict=False).is_relative_to(resolved_repo):
            raise ValueError("目标图片目录越出博客仓库")

        web_parts = (
            images_relative.parts[1:]
            if images_relative.parts[:1] == ("source",)
            else images_relative.parts
        )
        encoded_web_path = "/".join(quote(part, safe="") for part in web_parts)
        web_prefix = f"/{encoded_web_path}" if encoded_web_path else ""
        encoded_slug = quote(slug, safe="")
        asset_root: Path | None = None
        total_bytes = 0
        if content.images:
            configured_root = settings.github_pages_asset_root.expanduser()
            if configured_root.is_symlink():
                raise ValueError("GitHub Pages 受控图片目录不能是符号链接")
            try:
                asset_root = configured_root.resolve(strict=True)
            except OSError as exc:
                raise ValueError("GitHub Pages 受控图片目录不存在或不可读") from exc
            if not asset_root.is_dir():
                raise ValueError("GitHub Pages 受控图片目录不是目录")

        for raw_source in content.images:
            assert asset_root is not None
            source, metadata, digest = self._validate_source_image(raw_source, asset_root)
            total_bytes += metadata.st_size
            if total_bytes > settings.github_pages_max_total_image_bytes:
                raise ValueError("GitHub Pages 图片总大小超过配置上限")
            name_key = source.name.casefold()
            if name_key in destination_names:
                raise ValueError("多张图片映射到同一目标文件名，拒绝覆盖")
            destination_names.add(name_key)

            destination = image_root / source.name
            if self._path_exists(destination):
                raise ValueError("目标图片已存在，拒绝覆盖")
            site_path = f"{web_prefix}/{encoded_slug}/{quote(source.name, safe='')}"
            plans.append(
                _ImagePlan(
                    source=source,
                    destination=destination,
                    site_path=site_path,
                    size=metadata.st_size,
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                    digest=digest,
                )
            )
            if source.name not in body and raw_source not in body:
                body += f"\n\n![image]({site_path})"
        return plans, body

    @staticmethod
    def _source_open_flags() -> int:
        return (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )

    @classmethod
    def _validate_source_image(
        cls,
        raw_source: str,
        asset_root: Path,
    ) -> tuple[Path, os.stat_result, str]:
        candidate = Path(raw_source).expanduser()
        lexical = Path(os.path.abspath(candidate))
        if not lexical.is_relative_to(asset_root):
            raise ValueError(f"GitHub Pages 图片必须位于受控目录 {asset_root}")
        relative = lexical.relative_to(asset_root)
        if any(part.startswith(".") for part in relative.parts):
            raise ValueError("GitHub Pages 不接受隐藏图片文件或隐藏目录")

        cursor = asset_root
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise ValueError("GitHub Pages 不接受符号链接图片或符号链接目录")

        suffix = lexical.suffix.lower()
        expected_format = _ALLOWED_IMAGE_FORMATS.get(suffix)
        if expected_format is None:
            raise ValueError("GitHub Pages 图片格式只允许 JPEG/PNG/WebP/GIF")

        try:
            fd = os.open(lexical, cls._source_open_flags())
        except OSError as exc:
            raise ValueError("GitHub Pages 图片不存在或不可读") from exc
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("GitHub Pages 图片源必须是普通文件")
            if metadata.st_nlink != 1:
                raise ValueError("GitHub Pages 不接受硬链接图片")
            if metadata.st_size <= 0:
                raise ValueError("GitHub Pages 图片不能为空")
            if metadata.st_size > settings.github_pages_max_image_bytes:
                raise ValueError("GitHub Pages 单张图片超过配置上限")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", Image.DecompressionBombWarning)
                        with Image.open(handle) as image:
                            actual_format = image.format
                            image.verify()
                        handle.seek(0)
                        with Image.open(handle) as decoded:
                            decoded.load()
                except Exception as exc:
                    raise ValueError("GitHub Pages 图片无法通过实际解码校验") from exc
                if actual_format != expected_format:
                    raise ValueError("GitHub Pages 图片扩展名与实际格式不一致")
                handle.seek(0)
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
        finally:
            os.close(fd)

        source = lexical.resolve(strict=True)
        if source != lexical or not source.is_relative_to(asset_root):
            raise ValueError("GitHub Pages 图片路径含符号链接或越出受控目录")
        return source, metadata, digest

    @classmethod
    def _copy_planned_image(cls, plan: _ImagePlan) -> None:
        """Re-open with no-follow and reject source replacement after validation."""
        fd = os.open(plan.source, cls._source_open_flags())
        try:
            metadata = os.fstat(fd)
            identity = (metadata.st_dev, metadata.st_ino, metadata.st_size)
            if identity != (plan.device, plan.inode, plan.size):
                raise ValueError("GitHub Pages 图片在校验后发生变化")
            if metadata.st_nlink != 1:
                raise ValueError("GitHub Pages 图片在校验后变成硬链接")
            with os.fdopen(fd, "rb", closefd=False) as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
                if digest != plan.digest:
                    raise ValueError("GitHub Pages 图片在校验后发生变化")
                source.seek(0)
                with plan.destination.open("xb") as destination:
                    shutil.copyfileobj(source, destination)
        finally:
            os.close(fd)

    def _render(self, content: PublishContent, categories: list[str], body: str) -> str:
        return (
            HEXO_FRONTMATTER_TEMPLATE.format(
                title=_yaml_scalar(content.title),
                date=_yaml_scalar(datetime.now().isoformat(timespec="seconds")),
                tags=_yaml_list(content.tags),
                categories=_yaml_list(categories),
                extra_fields=self._extra_frontmatter(content),
            )
            + body
        )

    @staticmethod
    def _extra_frontmatter(content: PublishContent) -> str:
        extra = content.extra or {}
        lines: list[str] = []
        for key in ("cover", "top_img", "description", "keywords"):
            if extra.get(key) not in (None, "", []):
                lines.append(f"{key}: {_yaml_scalar(extra[key])}")
        return ("\n".join(lines) + "\n") if lines else ""

    @staticmethod
    def _article_url(slug: str) -> str:
        return f"{settings.github_pages_base_url.rstrip('/')}/{quote(slug, safe='')}/"

    @staticmethod
    def _valid_live_base_url(value: str) -> bool:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return False
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )

    @staticmethod
    def _failure(stage: str, error: str, *, retryable: bool = False) -> PublishResult:
        return PublishResult(
            success=False,
            effect_applied=False,
            retryable=retryable,
            error=error,
            raw_response={"stage": stage},
        )

    def _command_failure(self, stage: str, message: str, result: _CommandResult) -> PublishResult:
        if result.timed_out:
            detail = "执行超时"
        elif not result.started:
            detail = "命令无法启动"
        else:
            detail = f"退出码 {result.returncode}"
        return self._failure(stage, f"{message}: {detail}")

    @staticmethod
    def _remove_created_paths(paths: list[Path], repo: Path) -> None:
        """Remove only files this invocation created; never reset user git state."""
        resolved_repo = repo.resolve()
        for path in reversed(paths):
            try:
                if path.resolve(strict=False).is_relative_to(resolved_repo):
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _verified_sha(result: _CommandResult) -> str | None:
        if not result.ok or not result.stdout.strip():
            return None
        candidate = result.stdout.strip().splitlines()[0]
        return candidate if re.fullmatch(r"[0-9a-fA-F]{40,64}", candidate) else None

    @classmethod
    def _snapshot_rollback_paths(
        cls,
        repo: Path,
        paths: list[Path],
    ) -> list[_RollbackPath]:
        """Capture only paths this invocation is authorized to mutate."""
        resolved_repo = repo.resolve(strict=True)
        snapshots: list[_RollbackPath] = []
        seen: set[str] = set()
        for raw_path in paths:
            path = Path(os.path.abspath(raw_path))
            try:
                relative = path.relative_to(resolved_repo).as_posix()
            except ValueError as exc:
                raise ValueError("回滚目标越出博客仓库") from exc
            if relative in seen:
                raise ValueError("回滚目标路径重复")
            seen.add(relative)
            if path.is_symlink() or not path.resolve(strict=False).is_relative_to(
                resolved_repo
            ):
                raise ValueError(f"回滚目标路径不安全: {relative}")
            if path.exists():
                metadata = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"回滚目标不是普通文件: {relative}")
                snapshots.append(
                    _RollbackPath(
                        path=path,
                        relative=relative,
                        existed=True,
                        original_content=path.read_bytes(),
                        original_mode=stat.S_IMODE(metadata.st_mode),
                    )
                )
            else:
                snapshots.append(
                    _RollbackPath(
                        path=path,
                        relative=relative,
                        existed=False,
                        original_content=None,
                        original_mode=None,
                    )
                )
        return snapshots

    @classmethod
    def _seal_rollback_paths(cls, paths: list[_RollbackPath], repo: Path) -> None:
        """Record the exact bytes written by this invocation before build/git."""
        resolved_repo = repo.resolve(strict=True)
        for snapshot in paths:
            digest, size = cls._artifact_fingerprint(
                snapshot.path,
                resolved_repo,
                snapshot.relative,
            )
            snapshot.written_digest = digest
            snapshot.written_size = size

    @staticmethod
    def _artifact_fingerprint(
        path: Path,
        resolved_repo: Path,
        relative: str,
    ) -> tuple[str, int]:
        if path.is_symlink() or not path.resolve(strict=False).is_relative_to(resolved_repo):
            raise ValueError(f"本轮路径已被替换或越界: {relative}")
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError(f"本轮路径缺失或不可读: {relative}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"本轮路径不再是普通文件: {relative}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return digest, metadata.st_size

    async def _rollback_or_report(
        self,
        failure: PublishResult,
        *,
        repo: Path,
        preflight_head: str,
        preflight_status: str,
        paths: list[_RollbackPath],
        unstage: bool,
    ) -> PublishResult:
        rollback = await self._rollback_precommit(
            repo=repo,
            preflight_head=preflight_head,
            preflight_status=preflight_status,
            paths=paths,
            unstage=unstage,
        )
        if rollback.success:
            return failure
        raw = dict(failure.raw_response or {})
        raw["rollback_required"] = True
        return failure.model_copy(
            update={
                "retryable": False,
                "error": (
                    f"{failure.error or '发布失败'}；自动回滚无法安全完成："
                    f"{rollback.error}。请在仓库内运行 git status 并人工处理本次文章路径"
                ),
                "raw_response": raw,
            }
        )

    async def _rollback_precommit(
        self,
        *,
        repo: Path,
        preflight_head: str,
        preflight_status: str,
        paths: list[_RollbackPath],
        unstage: bool,
    ) -> _RollbackResult:
        """Restore this invocation's exact paths only when safety is provable."""
        timeout = settings.github_pages_git_timeout_seconds
        current_head_result = await self._run_argv(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repo,
            timeout_seconds=timeout,
        )
        current_head = self._verified_sha(current_head_result)
        if current_head is None:
            return _RollbackResult(False, "无法确认当前 HEAD，未执行文件恢复")
        if current_head.lower() != preflight_head.lower():
            return _RollbackResult(False, "HEAD 已变化，可能已生成本地 commit，未执行文件恢复")

        resolved_repo = repo.resolve(strict=True)
        for snapshot in paths:
            if snapshot.written_digest is None or snapshot.written_size is None:
                return _RollbackResult(False, f"缺少本轮文件指纹: {snapshot.relative}")
            if not self._path_exists(snapshot.path):
                if snapshot.existed:
                    return _RollbackResult(False, f"已有文件已消失: {snapshot.relative}")
                continue
            try:
                digest, size = self._artifact_fingerprint(
                    snapshot.path,
                    resolved_repo,
                    snapshot.relative,
                )
            except ValueError as exc:
                return _RollbackResult(False, str(exc))
            if (digest, size) != (snapshot.written_digest, snapshot.written_size):
                return _RollbackResult(False, f"本轮路径内容已被其他操作修改: {snapshot.relative}")

        relative_paths = [snapshot.relative for snapshot in paths]
        if unstage and relative_paths:
            staged = await self._staged_publish_paths(repo, relative_paths, timeout)
            if isinstance(staged, str):
                return _RollbackResult(False, staged)
            if staged:
                restore = await self._run_argv(
                    [
                        "git",
                        "restore",
                        "--staged",
                        f"--source={preflight_head}",
                        "--",
                        *sorted(staged),
                    ],
                    cwd=repo,
                    timeout_seconds=timeout,
                )
                if not restore.ok:
                    return _RollbackResult(False, "无法精确取消暂存，本轮文件未删除")
                remaining = await self._staged_publish_paths(repo, relative_paths, timeout)
                if isinstance(remaining, str):
                    return _RollbackResult(False, remaining)
                if remaining:
                    return _RollbackResult(False, "本轮路径仍有暂存内容，本轮文件未删除")

        try:
            for snapshot in paths:
                if snapshot.existed:
                    assert snapshot.original_content is not None
                    assert snapshot.original_mode is not None
                    self._atomic_restore_file(
                        snapshot.path,
                        snapshot.original_content,
                        snapshot.original_mode,
                    )
                else:
                    snapshot.path.unlink(missing_ok=True)
        except OSError as exc:
            return _RollbackResult(False, f"恢复本轮路径失败: {type(exc).__name__}")

        for snapshot in paths:
            if snapshot.existed:
                try:
                    restored = snapshot.path.read_bytes()
                except OSError:
                    return _RollbackResult(False, f"无法验证已恢复文件: {snapshot.relative}")
                if restored != snapshot.original_content:
                    return _RollbackResult(False, f"已有文件恢复校验失败: {snapshot.relative}")
            elif self._path_exists(snapshot.path):
                return _RollbackResult(False, f"本轮新文件删除校验失败: {snapshot.relative}")

        status = await self._run_argv(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repo,
            timeout_seconds=timeout,
        )
        if not status.ok:
            return _RollbackResult(False, "无法验证回滚后的 git 状态")
        if status.stdout != preflight_status:
            return _RollbackResult(False, "仓库存在非本任务改动，已原样保留")
        return _RollbackResult(True)

    async def _staged_publish_paths(
        self,
        repo: Path,
        relative_paths: list[str],
        timeout: int,
    ) -> set[str] | str:
        result = await self._run_argv(
            ["git", "diff", "--cached", "--name-only", "-z", "--", *relative_paths],
            cwd=repo,
            timeout_seconds=timeout,
        )
        if not result.ok:
            return "无法确认本轮路径的暂存状态，本轮文件未删除"
        staged = {value for value in result.stdout.split("\0") if value}
        unexpected = staged.difference(relative_paths)
        if unexpected:
            return "暂存查询返回非本任务路径，本轮文件未删除"
        return staged

    @staticmethod
    def _atomic_restore_file(path: Path, content: bytes, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{path.name}.ai-ops-rollback-",
                dir=path.parent,
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            temporary.chmod(mode)
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def _git_publish(
        self,
        *,
        repo: Path,
        paths: list[Path],
        slug: str,
        title: str,
        remote: str,
        branch: str,
    ) -> _GitPublishResult:
        relative_paths = [path.relative_to(repo).as_posix() for path in paths]
        timeout = settings.github_pages_git_timeout_seconds

        add = await self._run_argv(
            ["git", "add", "--", *relative_paths], cwd=repo, timeout_seconds=timeout
        )
        if not add.ok:
            return _GitPublishResult(success=False, error=self._git_error("git add", add))

        commit_message = f"post: {_safe_commit_title(title)} ({slug})"
        commit = await self._run_argv(
            [
                "git",
                "-c",
                f"core.hooksPath={os.devnull}",
                "commit",
                "--only",
                "-m",
                commit_message,
                "--",
                *relative_paths,
            ],
            cwd=repo,
            timeout_seconds=timeout,
        )
        if not commit.ok:
            return _GitPublishResult(success=False, error=self._git_error("git commit", commit))

        revision = await self._run_argv(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=repo, timeout_seconds=timeout
        )
        commit_sha = (
            revision.stdout.strip().splitlines()[0]
            if revision.ok and revision.stdout.strip()
            else ""
        )
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit_sha):
            return _GitPublishResult(success=False, error="无法确认本地 commit SHA")

        changed = await self._run_argv(
            [
                "git",
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "-r",
                "-z",
                commit_sha,
            ],
            cwd=repo,
            timeout_seconds=timeout,
        )
        if not changed.ok:
            return _GitPublishResult(
                success=False,
                commit_sha=commit_sha,
                error=self._git_error("git commit 路径验证", changed),
            )
        actual_paths = {value for value in changed.stdout.split("\0") if value}
        if actual_paths != set(relative_paths):
            return _GitPublishResult(
                success=False,
                commit_sha=commit_sha,
                error="本地 commit 包含非本任务路径，拒绝 push",
            )

        try:
            push = await self._run_argv(
                [
                    "git",
                    "push",
                    "--porcelain",
                    remote,
                    f"{commit_sha}:refs/heads/{branch}",
                ],
                cwd=repo,
                timeout_seconds=timeout,
            )
        except asyncio.CancelledError:
            return _GitPublishResult(
                success=False,
                commit_sha=commit_sha,
                error="git push 被取消，远端结果无法确认",
                outcome_uncertain=True,
            )

        if not push.started:
            return _GitPublishResult(
                success=False,
                commit_sha=commit_sha,
                error="git push 无法启动",
            )

        try:
            verification = await self._run_argv(
                [
                    "git",
                    "ls-remote",
                    "--exit-code",
                    remote,
                    f"refs/heads/{branch}",
                ],
                cwd=repo,
                timeout_seconds=timeout,
            )
        except asyncio.CancelledError:
            return _GitPublishResult(
                success=False,
                commit_sha=commit_sha,
                error="远端 commit 验证被取消，发布结果无法确认",
                outcome_uncertain=True,
            )

        remote_sha = ""
        if verification.returncode == 0 and verification.stdout.strip():
            remote_sha = verification.stdout.splitlines()[0].split()[0]
        if remote_sha.lower() == commit_sha.lower():
            return _GitPublishResult(success=True, commit_sha=commit_sha)

        verification_confirmed = (
            verification.started
            and not verification.timed_out
            and verification.returncode in {0, 2}
        )
        if not verification_confirmed:
            return _GitPublishResult(
                success=False,
                commit_sha=commit_sha,
                error="git push 已启动，但远端 commit 无法确认",
                outcome_uncertain=True,
            )

        if push.ok:
            error = "git push 返回成功，但目标分支未指向本次 commit"
        else:
            error = self._git_error("git push", push)
        return _GitPublishResult(success=False, commit_sha=commit_sha, error=error)

    @staticmethod
    def _git_error(stage: str, result: _CommandResult) -> str:
        if result.timed_out:
            return f"{stage} 超时"
        if not result.started:
            return f"{stage} 无法启动"
        return f"{stage} 失败（退出码 {result.returncode}）"

    async def _run_argv(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> _CommandResult:
        """Run a fixed argv command and always reap it on timeout/cancellation."""
        env = {
            key: value
            for key, value in os.environ.items()
            if key in _SUBPROCESS_ENV_ALLOWLIST
        }
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "NO_COLOR": "1",
            }
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            return _CommandResult(started=False, error=type(exc).__name__)

        try:
            stdout, _ = await asyncio.wait_for(
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
            return _CommandResult(started=True, timed_out=True, error="timeout")
        except Exception as exc:
            # In particular, a pipe error after `git push` starts cannot be
            # downgraded to a safe preflight failure.  Return a started result
            # so the caller performs remote SHA reconciliation.
            try:
                await self._stop_process(proc)
            except Exception:
                pass
            return _CommandResult(
                started=True,
                returncode=proc.returncode,
                error=type(exc).__name__,
            )

        captured = stdout[:_MAX_CAPTURE_BYTES].decode("utf-8", "replace")
        return _CommandResult(
            started=True,
            returncode=proc.returncode,
            stdout=captured,
        )

    async def _stop_process(self, proc: asyncio.subprocess.Process) -> None:
        await stop_process_group(
            proc,
            grace_seconds=_PROCESS_STOP_GRACE_SECONDS,
        )
