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
import secrets
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
from ..runtime.receipts import write_publish_receipt
from .base import PublisherBase
from .github_pages_gh import (
    GhPagesConfig,
    GitHubPagesGhVerifier,
    approved_gh_api_argv,
    github_repository_from_push_url,
)
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
_PUBLICATION_MARKER_ATTRIBUTE = "data-ai-ops-publication"
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


@dataclass(slots=True, frozen=True)
class _RepositoryLeaf:
    relative: str
    parent_device: int
    parent_inode: int
    device: int
    inode: int
    digest: str
    size: int


@dataclass(slots=True)
class _RollbackPath:
    path: Path
    relative: str
    existed: bool
    original_content: bytes | None
    original_mode: int | None
    written_digest: str | None = None
    written_size: int | None = None
    written_git_mode: str | None = None
    approved_leaf: _RepositoryLeaf | None = None


@dataclass(slots=True, frozen=True)
class _RollbackResult:
    success: bool
    error: str | None = None


class _RepositoryFiles:
    """No-follow file capability rooted at one already-approved repository.

    Every mutable leaf operation is resolved component-by-component from the
    repository directory descriptor.  A parent replaced by a symlink between
    preflight and mutation is therefore rejected by ``O_NOFOLLOW`` instead of
    redirecting a write, replace, or unlink outside the repository.
    """

    def __init__(self, repo: Path) -> None:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        required_dir_fd = {os.open, os.mkdir, os.stat, os.unlink}
        if (
            os.name != "posix"
            or not nofollow
            or not directory
            or not required_dir_fd.issubset(os.supports_dir_fd)
            or os.rename not in os.supports_dir_fd
        ):
            raise OSError("当前操作系统不支持安全的仓库内文件能力")

        self.root = repo.resolve(strict=True)
        flags = os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)
        self._directory_flags = flags
        self._file_read_flags = (
            os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
        )
        self._root_fd = os.open(self.root, flags)
        if not stat.S_ISDIR(os.fstat(self._root_fd).st_mode):
            os.close(self._root_fd)
            raise OSError("博客仓库根路径不是目录")

    def close(self) -> None:
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __enter__(self) -> _RepositoryFiles:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def relative_path(self, path: Path) -> str:
        lexical = Path(os.path.abspath(path))
        try:
            relative = lexical.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("文件目标越出博客仓库") from exc
        return self._validated_parts(relative.as_posix())[1]

    @staticmethod
    def _validated_parts(relative: str) -> tuple[tuple[str, ...], str]:
        candidate = Path(relative)
        parts = candidate.parts
        if (
            not relative
            or candidate.is_absolute()
            or not parts
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError("仓库内文件路径不安全")
        return parts, candidate.as_posix()

    def _open_parent(self, relative: str, *, create: bool) -> tuple[int, str]:
        parts, _ = self._validated_parts(relative)
        current = os.dup(self._root_fd)
        try:
            for component in parts[:-1]:
                if create:
                    try:
                        os.mkdir(component, 0o755, dir_fd=current)
                    except FileExistsError:
                        pass
                child = os.open(component, self._directory_flags, dir_fd=current)
                if not stat.S_ISDIR(os.fstat(child).st_mode):
                    os.close(child)
                    raise OSError("仓库内父路径不是目录")
                os.close(current)
                current = child
            return current, parts[-1]
        except BaseException:
            os.close(current)
            raise

    def lstat(self, relative: str) -> os.stat_result | None:
        try:
            parent, name = self._open_parent(relative, create=False)
        except FileNotFoundError:
            return None
        try:
            try:
                return os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return None
        finally:
            os.close(parent)

    def _open_regular(self, relative: str) -> tuple[int, os.stat_result]:
        parent, name = self._open_parent(relative, create=False)
        try:
            fd = os.open(name, self._file_read_flags, dir_fd=parent)
        finally:
            os.close(parent)
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(fd)
            raise OSError("仓库内目标不是普通文件")
        return fd, metadata

    def read_regular(self, relative: str) -> tuple[bytes, os.stat_result]:
        fd, metadata = self._open_regular(relative)
        try:
            with os.fdopen(fd, "rb", closefd=False) as handle:
                return handle.read(), metadata
        finally:
            os.close(fd)

    @staticmethod
    def _same_parent(parent_fd: int, approved: _RepositoryLeaf) -> bool:
        metadata = os.fstat(parent_fd)
        return (metadata.st_dev, metadata.st_ino) == (
            approved.parent_device,
            approved.parent_inode,
        )

    @staticmethod
    def _same_leaf(metadata: os.stat_result, approved: _RepositoryLeaf) -> bool:
        return (metadata.st_dev, metadata.st_ino) == (
            approved.device,
            approved.inode,
        )

    @staticmethod
    def _digest_fd(fd: int) -> str:
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()

    def fingerprint(
        self,
        relative: str,
        *,
        approved: _RepositoryLeaf | None = None,
    ) -> tuple[str, int, int, _RepositoryLeaf]:
        parent, name = self._open_parent(relative, create=False)
        try:
            if approved is not None and not self._same_parent(parent, approved):
                raise OSError("仓库内父目录已被替换")
            fd = os.open(name, self._file_read_flags, dir_fd=parent)
            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise OSError("仓库内目标不是普通文件")
                if approved is not None and not self._same_leaf(metadata, approved):
                    raise OSError("仓库内文件已被替换")
                digest = self._digest_fd(fd)
                if approved is not None and (
                    digest != approved.digest or metadata.st_size != approved.size
                ):
                    raise OSError("仓库内文件内容已被替换")
                parent_metadata = os.fstat(parent)
                captured = _RepositoryLeaf(
                    relative=relative,
                    parent_device=parent_metadata.st_dev,
                    parent_inode=parent_metadata.st_ino,
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                    digest=digest,
                    size=metadata.st_size,
                )
            finally:
                os.close(fd)
        finally:
            os.close(parent)
        return digest, metadata.st_size, stat.S_IMODE(metadata.st_mode), captured

    def create_bytes(self, relative: str, content: bytes) -> _RepositoryLeaf:
        parent, name = self._open_parent(relative, create=True)
        fd: int | None = None
        created = False
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_BINARY", 0)
            )
            fd = os.open(name, flags, 0o644, dir_fd=parent)
            created = True
            with os.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(content)
            metadata = os.fstat(fd)
            parent_metadata = os.fstat(parent)
            return _RepositoryLeaf(
                relative=relative,
                parent_device=parent_metadata.st_dev,
                parent_inode=parent_metadata.st_ino,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                digest=hashlib.sha256(content).hexdigest(),
                size=len(content),
            )
        except BaseException:
            if fd is not None:
                os.close(fd)
                fd = None
            if created:
                try:
                    os.unlink(name, dir_fd=parent)
                except OSError:
                    pass
            raise
        finally:
            if fd is not None:
                os.close(fd)
            os.close(parent)

    def copy_from_fd(
        self,
        relative: str,
        source_fd: int,
        *,
        digest: str,
        size: int,
    ) -> _RepositoryLeaf:
        parent, name = self._open_parent(relative, create=True)
        destination_fd: int | None = None
        created = False
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_BINARY", 0)
            )
            destination_fd = os.open(name, flags, 0o644, dir_fd=parent)
            created = True
            with (
                os.fdopen(source_fd, "rb", closefd=False) as source,
                os.fdopen(destination_fd, "wb", closefd=False) as destination,
            ):
                shutil.copyfileobj(source, destination)
            metadata = os.fstat(destination_fd)
            if metadata.st_size != size:
                raise OSError("GitHub Pages 图片写入大小不一致")
            parent_metadata = os.fstat(parent)
            return _RepositoryLeaf(
                relative=relative,
                parent_device=parent_metadata.st_dev,
                parent_inode=parent_metadata.st_ino,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                digest=digest,
                size=size,
            )
        except BaseException:
            if destination_fd is not None:
                os.close(destination_fd)
                destination_fd = None
            if created:
                try:
                    os.unlink(name, dir_fd=parent)
                except OSError:
                    pass
            raise
        finally:
            if destination_fd is not None:
                os.close(destination_fd)
            os.close(parent)

    def unlink(self, relative: str, *, missing_ok: bool = False) -> None:
        try:
            parent, name = self._open_parent(relative, create=False)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        try:
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                if not missing_ok:
                    raise
        finally:
            os.close(parent)

    def _approved_leaf_fd(
        self,
        approved: _RepositoryLeaf,
    ) -> tuple[int, str, int]:
        parent, name = self._open_parent(approved.relative, create=False)
        fd: int | None = None
        try:
            if not self._same_parent(parent, approved):
                raise OSError("仓库内父目录已被替换")
            fd = os.open(name, self._file_read_flags, dir_fd=parent)
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode) or not self._same_leaf(metadata, approved):
                raise OSError("仓库内文件已被替换")
            digest = self._digest_fd(fd)
            if digest != approved.digest or metadata.st_size != approved.size:
                raise OSError("仓库内文件内容已被替换")
            return parent, name, fd
        except BaseException:
            if fd is not None:
                os.close(fd)
            os.close(parent)
            raise

    def unlink_approved(self, approved: _RepositoryLeaf) -> None:
        parent, name, fd = self._approved_leaf_fd(approved)
        try:
            os.close(fd)
            fd = -1
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not self._same_leaf(current, approved):
                raise OSError("仓库内文件在删除前被替换")
            os.unlink(name, dir_fd=parent)
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise OSError("仓库内文件删除后仍存在")
        finally:
            if fd >= 0:
                os.close(fd)
            os.close(parent)

    def atomic_restore(
        self,
        approved: _RepositoryLeaf,
        content: bytes,
        mode: int,
    ) -> None:
        parent, name, approved_fd = self._approved_leaf_fd(approved)
        temporary_name = f".{name}.ai-ops-rollback-{secrets.token_hex(16)}"
        temporary_fd: int | None = None
        temporary_exists = False
        try:
            os.close(approved_fd)
            approved_fd = -1
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not self._same_leaf(current, approved):
                raise OSError("仓库内文件在恢复前被替换")
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_BINARY", 0)
            )
            temporary_fd = os.open(temporary_name, flags, 0o600, dir_fd=parent)
            temporary_exists = True
            with os.fdopen(temporary_fd, "wb", closefd=False) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.fchmod(temporary_fd, mode)
            os.close(temporary_fd)
            temporary_fd = None
            os.replace(
                temporary_name,
                name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
            )
            temporary_exists = False
            restored_fd = os.open(name, self._file_read_flags, dir_fd=parent)
            try:
                restored_metadata = os.fstat(restored_fd)
                restored_digest = self._digest_fd(restored_fd)
                if (
                    restored_digest != hashlib.sha256(content).hexdigest()
                    or restored_metadata.st_size != len(content)
                    or stat.S_IMODE(restored_metadata.st_mode) != mode
                ):
                    raise OSError("仓库内文件恢复后校验失败")
            finally:
                os.close(restored_fd)
        finally:
            if approved_fd >= 0:
                os.close(approved_fd)
            if temporary_fd is not None:
                os.close(temporary_fd)
            if temporary_exists:
                try:
                    os.unlink(temporary_name, dir_fd=parent)
                except OSError:
                    pass
            os.close(parent)


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


def _stage_verified_executable(
    source: Path,
) -> tuple[tempfile.TemporaryDirectory[str], Path, str]:
    """Copy one opened executable into a private runtime while hashing its bytes."""

    runtime = tempfile.TemporaryDirectory(prefix="ai-ops-gh-binary-")
    root = Path(runtime.name)
    destination = root / "gh"
    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    source_flags |= getattr(os, "O_NOFOLLOW", 0)
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    destination_flags |= getattr(os, "O_CLOEXEC", 0)
    digest = hashlib.sha256()
    copied = 0
    try:
        os.chmod(root, 0o700)
        source_fd = os.open(source, source_flags)
        try:
            before = os.fstat(source_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > 512 * 1024 * 1024:
                raise OSError("invalid executable")
            destination_fd = os.open(destination, destination_flags, 0o500)
            try:
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > 512 * 1024 * 1024:
                        raise OSError("executable exceeds size contract")
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(destination_fd, view)
                        if written <= 0:
                            raise OSError("short executable write")
                        view = view[written:]
                after = os.fstat(source_fd)
                if copied != before.st_size or (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
                    raise OSError("executable changed while being copied")
                os.fchmod(destination_fd, 0o500)
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)
        finally:
            os.close(source_fd)
        return runtime, destination, digest.hexdigest()
    except Exception:
        runtime.cleanup()
        raise


def _verified_executable_digest(path: Path) -> str | None:
    """Hash one no-follow regular executable and reject concurrent mutation."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > 512 * 1024 * 1024:
                return None
            digest = hashlib.sha256()
            copied = 0
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > 512 * 1024 * 1024:
                    return None
                digest.update(chunk)
            after = os.fstat(fd)
            if copied != before.st_size or (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
                return None
            return digest.hexdigest()
        finally:
            os.close(fd)
    except OSError:
        return None


class GitHubPagesPublisher(PublisherBase):
    """Publish Markdown through fixed Hexo and git CLI contracts."""

    platform = Platform.GITHUB_PAGES
    kind = PublisherKind.HEXO

    def _cleanup_gh_runtime(self) -> None:
        runtime = getattr(self, "_github_pages_gh_runtime", None)
        if runtime is not None:
            runtime.cleanup()
            del self._github_pages_gh_runtime
        for attribute in (
            "_github_pages_gh_binary_path",
            "_github_pages_gh_binary_digest",
        ):
            if hasattr(self, attribute):
                delattr(self, attribute)

    async def login(self, account_id: int, credential: dict) -> bool:
        """Probe configured remote/ref readability without mutating the remote."""
        del account_id, credential
        self._cleanup_gh_runtime()
        repo = settings.github_pages_path.expanduser().resolve()
        remote = settings.github_pages_remote
        branch = settings.github_pages_branch
        if not self._is_git_repo(repo) or not self._valid_git_target(remote, branch):
            return False
        if not await self._remote_exists(repo, remote):
            return False
        remote_url, _ = await self._push_url(repo, remote)
        if remote_url is None:
            return False
        transport_pin, transport_target = self._transport_url_pin(remote_url)
        result = await self._run_argv(
            [
                "git",
                *transport_pin,
                "ls-remote",
                "--exit-code",
                "--",
                transport_target,
                f"refs/heads/{branch}",
            ],
            cwd=repo,
            timeout_seconds=settings.github_pages_git_timeout_seconds,
        )
        if not result.ok:
            return False
        if not bool(getattr(settings, "github_pages_gh_verify_enabled", False)):
            return True
        head = await self._run_argv(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repo,
            timeout_seconds=settings.github_pages_git_timeout_seconds,
        )
        local_sha = self._verified_sha(head)
        remote_sha = self._verified_remote_sha(result, branch)
        if local_sha is None or remote_sha is None or local_sha.lower() != remote_sha.lower():
            return False
        verifier, _, _ = await self._prepare_gh_verifier(
            repo,
            remote,
            branch,
            remote_url=remote_url,
        )
        if verifier is None:
            return False
        try:
            preflight = await verifier.preflight(remote_url=remote_url)
            return preflight.success
        finally:
            self._cleanup_gh_runtime()

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
        self._cleanup_gh_runtime()
        repo = settings.github_pages_path.expanduser().resolve()
        if not repo.is_dir():
            return self._failure("preflight", "配置的博客仓库不存在或不是目录")
        if not self._is_git_repo(repo):
            return self._failure("preflight", "配置的博客仓库不是可用的 git 仓库")
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
            marker = self._publication_marker(rendered)
            rendered = self._embed_publication_marker(rendered, marker)
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
        finally:
            self._cleanup_gh_runtime()

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
        marker = self._publication_marker(rendered)
        rendered = self._embed_publication_marker(rendered, marker)

        if not await self._remote_exists(repo, remote):
            return self._failure("preflight", "配置的 git remote 不存在")
        remote_url, remote_url_error = await self._push_url(repo, remote)
        if remote_url is None:
            return self._failure(
                "preflight",
                remote_url_error or "无法确认 git push remote identity",
            )

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

        transport_pin, transport_target = self._transport_url_pin(remote_url)
        remote_head_result = await self._run_argv(
            [
                "git",
                *transport_pin,
                "ls-remote",
                "--exit-code",
                "--",
                transport_target,
                f"refs/heads/{branch}",
            ],
            cwd=repo,
            timeout_seconds=settings.github_pages_git_timeout_seconds,
        )
        remote_head = self._verified_remote_sha(remote_head_result, branch)
        if remote_head is None:
            return self._command_failure(
                "preflight",
                "无法确认发布前远端分支基线",
                remote_head_result,
            )
        if remote_head.lower() != preflight_head.lower():
            return self._failure(
                "preflight",
                "本地 HEAD 与远端发布分支不一致，拒绝携带未审核 commit 发布",
            )

        verifier: GitHubPagesGhVerifier | None = None
        if bool(getattr(settings, "github_pages_gh_verify_enabled", False)):
            verifier, remote_url, verifier_error = await self._prepare_gh_verifier(
                repo,
                remote,
                branch,
                remote_url=remote_url,
            )
            if verifier is None or remote_url is None:
                return self._failure(
                    "preflight",
                    verifier_error or "GitHub Pages gh verification preflight is unavailable",
                )
            gh_preflight = await verifier.preflight(remote_url=remote_url)
            if not gh_preflight.success:
                return self._failure(
                    "preflight",
                    gh_preflight.error or "GitHub Pages gh verification preflight failed",
                )

        publish_paths = [post_path, *(plan.destination for plan in image_plans)]
        rendered_bytes = rendered.encode("utf-8")
        expected_artifacts = {
            post_relative.as_posix(): (
                hashlib.sha256(rendered_bytes).hexdigest(),
                len(rendered_bytes),
            ),
            **{
                plan.destination.relative_to(repo).as_posix(): (plan.digest, plan.size)
                for plan in image_plans
            },
        }
        try:
            rollback_paths = self._snapshot_rollback_paths(repo, publish_paths)
        except (OSError, ValueError) as exc:
            return self._failure("preflight", f"无法建立发布回滚快照: {exc}")

        created_paths: list[_RepositoryLeaf] = []
        try:
            with _RepositoryFiles(repo) as repository_files:
                created_paths.append(
                    repository_files.create_bytes(post_relative.as_posix(), rendered_bytes)
                )
                for plan in image_plans:
                    image_relative = repository_files.relative_path(plan.destination)
                    created_paths.append(
                        self._copy_planned_image(plan, repository_files, image_relative)
                    )
        except (FileExistsError, OSError, ValueError) as exc:
            self._remove_created_paths(created_paths, repo)
            return self._failure("write", f"写入发布产物失败: {type(exc).__name__}")

        try:
            self._seal_rollback_paths(
                rollback_paths,
                repo,
                expected_artifacts=expected_artifacts,
                created_artifacts={item.relative: item for item in created_paths},
            )
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

        sealed_error = self._sealed_paths_error(rollback_paths, repo)
        if sealed_error is not None:
            return await self._rollback_or_report(
                self._failure("build", sealed_error),
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
            push_target=remote_url,
            branch=branch,
            preflight_head=preflight_head,
            sealed_paths=rollback_paths,
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
        assert commit_sha is not None
        accepted = PublishResult(
            success=verifier is None,
            effect_applied=True,
            retryable=False,
            platform_post_id=commit_sha,
            platform_url=article_url,
            raw_response={
                "state": "accepted",
                "slug": slug,
                "commit_sha": commit_sha,
                "remote": remote,
                "branch": branch,
                "post_path": post_relative.as_posix(),
            },
        )
        accepted = self._journal_result(content, accepted, required=True)
        if accepted.error is not None:
            return accepted
        if verifier is None:
            return accepted

        try:
            deployment = await verifier.wait_for_deployment(commit_sha)
        except Exception:
            return self._journal_result(
                content,
                PublishResult(
                    success=False,
                    effect_applied=True,
                    retryable=False,
                    outcome_uncertain=True,
                    platform_post_id=commit_sha,
                    platform_url=article_url,
                    error="GitHub Pages deployment verification failed unexpectedly",
                    raw_response={**accepted.raw_response, "state": "accepted"},
                ),
            )
        if not deployment.success:
            return self._journal_result(
                content,
                PublishResult(
                    success=False,
                    effect_applied=True,
                    retryable=False,
                    outcome_uncertain=deployment.outcome_uncertain,
                    platform_post_id=commit_sha,
                    platform_url=article_url,
                    error=deployment.error or "GitHub Pages deployment was not confirmed",
                    raw_response={**accepted.raw_response, "state": "accepted"},
                ),
            )

        deployed = accepted.model_copy(
            update={
                "raw_response": {**accepted.raw_response, "state": "deployed"},
            }
        )
        self._journal_result(content, deployed)

        try:
            site = await verifier.confirm_site()
        except Exception:
            return self._journal_result(
                content,
                PublishResult(
                    success=False,
                    effect_applied=True,
                    retryable=False,
                    outcome_uncertain=True,
                    platform_post_id=commit_sha,
                    platform_url=article_url,
                    error="GitHub Pages deployed site verification failed unexpectedly",
                    raw_response={**accepted.raw_response, "state": "deployed"},
                ),
            )
        if not site.success:
            return self._journal_result(
                content,
                PublishResult(
                    success=False,
                    effect_applied=True,
                    retryable=False,
                    outcome_uncertain=True,
                    platform_post_id=commit_sha,
                    platform_url=article_url,
                    error=site.error or "GitHub Pages deployed site metadata was not confirmed",
                    raw_response={**accepted.raw_response, "state": "deployed"},
                ),
            )

        try:
            readback = await verifier.wait_for_readback(article_url=article_url, marker=marker)
        except Exception:
            return self._journal_result(
                content,
                PublishResult(
                    success=False,
                    effect_applied=True,
                    retryable=False,
                    outcome_uncertain=True,
                    platform_post_id=commit_sha,
                    platform_url=article_url,
                    error="GitHub Pages public readback failed unexpectedly",
                    raw_response={**accepted.raw_response, "state": "deployed"},
                ),
            )
        if not readback.success:
            return self._journal_result(
                content,
                PublishResult(
                    success=False,
                    effect_applied=True,
                    retryable=False,
                    outcome_uncertain=True,
                    platform_post_id=commit_sha,
                    platform_url=article_url,
                    error=readback.error or "GitHub Pages public marker was not confirmed",
                    raw_response={**accepted.raw_response, "state": "deployed"},
                ),
            )

        verified = accepted.model_copy(
            update={
                "success": True,
                "raw_response": {
                    **accepted.raw_response,
                    "state": "verified",
                    "marker_sha256": marker,
                },
            }
        )
        return self._journal_result(content, verified)

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

    async def _prepare_gh_verifier(
        self,
        repo: Path,
        remote: str,
        branch: str,
        *,
        remote_url: str,
    ) -> tuple[GitHubPagesGhVerifier | None, str | None, str | None]:
        """Bind one configured repository to one exact push URL before writes."""

        secret = getattr(settings, "github_pages_gh_token", "")
        reveal = getattr(secret, "get_secret_value", None)
        token = reveal() if callable(reveal) else str(secret)
        configured_binary = str(getattr(settings, "github_pages_gh_bin", "gh"))
        discovered_binary = shutil.which(configured_binary)
        if discovered_binary is None:
            return None, None, "GitHub CLI executable is unavailable"
        runtime: tempfile.TemporaryDirectory[str] | None = None
        try:
            binary_path = Path(discovered_binary).resolve(strict=True)
            runtime, staged_binary, binary_digest = await asyncio.to_thread(
                _stage_verified_executable,
                binary_path,
            )
        except OSError:
            return None, None, "GitHub CLI executable identity cannot be verified"
        expected_binary_digest = str(getattr(settings, "github_pages_gh_sha256", ""))
        if (
            re.fullmatch(r"[0-9a-f]{64}", expected_binary_digest) is None
            or binary_digest != expected_binary_digest
        ):
            assert runtime is not None
            runtime.cleanup()
            return None, None, "GitHub CLI executable does not match the approved SHA-256"
        assert runtime is not None
        self._cleanup_gh_runtime()
        self._github_pages_gh_runtime = runtime
        self._github_pages_gh_binary_path = str(staged_binary)
        self._github_pages_gh_binary_digest = expected_binary_digest

        config = GhPagesConfig(
            repository=str(getattr(settings, "github_pages_repository", "")),
            branch=branch,
            base_url=settings.github_pages_base_url,
            expected_version=str(getattr(settings, "github_pages_gh_version", "2.97.0")),
            token_configured=bool(token.strip()),
            command_timeout_seconds=settings.github_pages_git_timeout_seconds,
            deploy_timeout_seconds=int(
                getattr(settings, "github_pages_deploy_timeout_seconds", 600)
            ),
            poll_seconds=int(getattr(settings, "github_pages_verify_poll_seconds", 5)),
            readback_timeout_seconds=int(
                getattr(settings, "github_pages_readback_timeout_seconds", 120)
            ),
            readback_request_timeout_seconds=int(
                getattr(settings, "github_pages_readback_request_timeout_seconds", 10)
            ),
            readback_max_bytes=int(
                getattr(settings, "github_pages_readback_max_response_bytes", 2 * 1024 * 1024)
            ),
            binary=str(staged_binary),
        )
        return GitHubPagesGhVerifier(config, cwd=repo, runner=self._run_argv), remote_url, None

    async def _push_url(self, repo: Path, remote: str) -> tuple[str | None, str | None]:
        result = await self._run_argv(
            ["git", "remote", "get-url", "--push", "--all", remote],
            cwd=repo,
            timeout_seconds=settings.github_pages_git_timeout_seconds,
        )
        if not result.ok:
            return None, "无法确认 git push remote identity"
        values = result.stdout.splitlines()
        if len(values) != 1 or not values[0] or values[0] != values[0].strip():
            return None, "GitHub Pages publishing requires exactly one push URL"
        value = values[0]
        if (
            not value
            or value.startswith("-")
            or len(value) > 4096
            or "=" in value
            or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        ):
            return None, "git push URL does not satisfy the safe target contract"
        # Production network writes are restricted to ordinary, credential-free
        # github.com HTTPS/SSH URLs. Absolute paths remain available for isolated
        # local/bare-repository tests without enabling Git remote helpers.
        if github_repository_from_push_url(value) is None and not Path(value).is_absolute():
            return None, "git push URL is not an approved github.com or local target"

        # Git applies url.*.insteadOf/pushInsteadOf even when a concrete URL is
        # passed to a transport command.  Pin the longest possible (exact) URL
        # mapping and prove the resulting transport identity before any write.
        transport_pin, transport_target = self._transport_url_pin(value)
        resolved = await self._run_argv(
            [
                "git",
                *transport_pin,
                "ls-remote",
                "--get-url",
                "--",
                transport_target,
            ],
            cwd=repo,
            timeout_seconds=settings.github_pages_git_timeout_seconds,
        )
        if not resolved.ok or resolved.stdout.splitlines() != [value]:
            return None, "git push URL could not be pinned against Git URL rewrites"
        return value, None

    @staticmethod
    def _transport_url_pin(target: str) -> tuple[list[str], str]:
        """Return a one-use unguessable alias that rewrites exactly once to ``target``.

        Git chooses the longest ``insteadOf`` prefix and does not recursively
        rewrite the replacement.  A random full-length alias therefore wins
        over ambient rules while preventing a later exact-target rule from
        retargeting the already-approved destination.
        """

        alias = f"ai-ops-transport-{secrets.token_hex(32)}://repository"
        return (
            [
                "-c",
                f"url.{target}.insteadOf={alias}",
                "-c",
                f"url.{target}.pushInsteadOf={alias}",
            ],
            alias,
        )

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
            configured_root = (
                settings.agent_asset_vault_root
                if content.exact_approval
                else settings.github_pages_asset_root
            ).expanduser()
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

    def _copy_planned_image(
        self,
        plan: _ImagePlan,
        repository_files: _RepositoryFiles,
        relative: str,
    ) -> _RepositoryLeaf:
        """Re-open with no-follow and reject source replacement after validation."""
        fd = os.open(plan.source, self._source_open_flags())
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
                return repository_files.copy_from_fd(
                    relative,
                    fd,
                    digest=plan.digest,
                    size=plan.size,
                )
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
    def _publication_marker(rendered: str) -> str:
        """Bind public readback to the exact bytes approved for this source file."""

        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()

    @staticmethod
    def _embed_publication_marker(rendered: str, marker: str) -> str:
        return (
            rendered.rstrip()
            + f'\n\n<span hidden {_PUBLICATION_MARKER_ATTRIBUTE}="{marker}"></span>\n'
        )

    def _journal_result(
        self,
        content: PublishContent,
        result: PublishResult,
        *,
        required: bool = False,
    ) -> PublishResult:
        """Persist each monotonic delivery state before waiting for the next proof."""

        path = write_publish_receipt(
            job_id=content.job_id,
            operation_id=content.operation_id,
            publisher_kind=self.kind.value,
            result=result,
        )
        has_runtime_identity = content.job_id is not None or content.operation_id is not None
        if required and has_runtime_identity and path is None:
            return result.model_copy(
                update={
                    "success": False,
                    "retryable": False,
                    "error": "远端 source commit 已接受，但本地 durable receipt 写入失败",
                }
            )
        return result

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
    def _remove_created_paths(paths: list[_RepositoryLeaf], repo: Path) -> None:
        """Remove only files this invocation created; never reset user git state."""
        try:
            with _RepositoryFiles(repo) as repository_files:
                for approved in reversed(paths):
                    try:
                        repository_files.unlink_approved(approved)
                    except OSError:
                        pass
        except OSError:
            pass

    @staticmethod
    def _verified_sha(result: _CommandResult) -> str | None:
        if not result.ok:
            return None
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            return None
        candidate = lines[0]
        return candidate if re.fullmatch(r"[0-9a-fA-F]{40,64}", candidate) else None

    @staticmethod
    def _commit_has_exact_parent(
        result: _CommandResult,
        *,
        commit_sha: str,
        expected_parent: str,
    ) -> bool:
        """Accept only one non-merge commit rooted at the preflight HEAD."""

        if not result.ok:
            return False
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            return False
        fields = lines[0].split()
        if len(fields) != 2 or not all(
            re.fullmatch(r"[0-9a-fA-F]{40,64}", field) for field in fields
        ):
            return False
        return (
            fields[0].lower() == commit_sha.lower() and fields[1].lower() == expected_parent.lower()
        )

    @staticmethod
    def _verified_remote_sha(result: _CommandResult, branch: str) -> str | None:
        if not result.ok:
            return None
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 1:
            return None
        fields = lines[0].split()
        expected_ref = f"refs/heads/{branch}"
        if len(fields) != 2 or fields[1] != expected_ref:
            return None
        return fields[0] if re.fullmatch(r"[0-9a-fA-F]{40,64}", fields[0]) else None

    @classmethod
    def _snapshot_rollback_paths(
        cls,
        repo: Path,
        paths: list[Path],
    ) -> list[_RollbackPath]:
        """Capture only paths this invocation is authorized to mutate."""
        snapshots: list[_RollbackPath] = []
        seen: set[str] = set()
        with _RepositoryFiles(repo) as repository_files:
            for raw_path in paths:
                path = Path(os.path.abspath(raw_path))
                relative = repository_files.relative_path(path)
                if relative in seen:
                    raise ValueError("回滚目标路径重复")
                seen.add(relative)
                metadata = repository_files.lstat(relative)
                if metadata is None:
                    snapshots.append(
                        _RollbackPath(
                            path=path,
                            relative=relative,
                            existed=False,
                            original_content=None,
                            original_mode=None,
                        )
                    )
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError(f"回滚目标不是普通文件: {relative}")
                content, opened_metadata = repository_files.read_regular(relative)
                snapshots.append(
                    _RollbackPath(
                        path=path,
                        relative=relative,
                        existed=True,
                        original_content=content,
                        original_mode=stat.S_IMODE(opened_metadata.st_mode),
                    )
                )
        return snapshots

    @classmethod
    def _seal_rollback_paths(
        cls,
        paths: list[_RollbackPath],
        repo: Path,
        *,
        expected_artifacts: dict[str, tuple[str, int]] | None = None,
        created_artifacts: dict[str, _RepositoryLeaf] | None = None,
    ) -> None:
        """Record the exact bytes written by this invocation before build/git."""
        with _RepositoryFiles(repo) as repository_files:
            for snapshot in paths:
                digest, size, mode, captured = cls._artifact_fingerprint(
                    repository_files,
                    snapshot.relative,
                    approved=(created_artifacts or {}).get(snapshot.relative),
                )
                if expected_artifacts is not None:
                    expected = expected_artifacts.get(snapshot.relative)
                    if expected is None or (digest, size) != expected:
                        raise ValueError(f"本轮路径与批准内容不一致: {snapshot.relative}")
                    digest, size = expected
                snapshot.written_digest = digest
                snapshot.written_size = size
                snapshot.written_git_mode = "100755" if mode & 0o111 else "100644"
                snapshot.approved_leaf = captured

    @classmethod
    def _sealed_paths_error(cls, paths: list[_RollbackPath], repo: Path) -> str | None:
        """Detect build hooks or lock-ignoring writers before staging."""

        try:
            repository_files = _RepositoryFiles(repo)
        except OSError as exc:
            return f"无法安全打开博客仓库: {type(exc).__name__}"
        with repository_files:
            for snapshot in paths:
                if (
                    snapshot.written_digest is None
                    or snapshot.written_size is None
                    or snapshot.written_git_mode is None
                    or snapshot.approved_leaf is None
                ):
                    return f"缺少本轮路径批准指纹: {snapshot.relative}"
                try:
                    digest, size, mode, _ = cls._artifact_fingerprint(
                        repository_files,
                        snapshot.relative,
                        approved=snapshot.approved_leaf,
                    )
                except (OSError, ValueError):
                    return f"本轮路径在 build 后发生变化: {snapshot.relative}"
                git_mode = "100755" if mode & 0o111 else "100644"
                if (digest, size, git_mode) != (
                    snapshot.written_digest,
                    snapshot.written_size,
                    snapshot.written_git_mode,
                ):
                    return f"本轮路径在 build 后发生变化: {snapshot.relative}"
        return None

    @staticmethod
    def _artifact_fingerprint(
        repository_files: _RepositoryFiles,
        relative: str,
        *,
        approved: _RepositoryLeaf | None = None,
    ) -> tuple[str, int, int, _RepositoryLeaf]:
        try:
            return repository_files.fingerprint(relative, approved=approved)
        except OSError as exc:
            raise ValueError(f"本轮路径缺失或不可读: {relative}") from exc

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

        try:
            repository_files = _RepositoryFiles(repo)
        except OSError as exc:
            return _RollbackResult(False, f"无法安全打开博客仓库: {type(exc).__name__}")
        with repository_files:
            for snapshot in paths:
                if (
                    snapshot.written_digest is None
                    or snapshot.written_size is None
                    or snapshot.approved_leaf is None
                ):
                    return _RollbackResult(False, f"缺少本轮文件指纹: {snapshot.relative}")
                try:
                    metadata = repository_files.lstat(snapshot.relative)
                except OSError as exc:
                    return _RollbackResult(
                        False,
                        f"本轮路径缺失或不可读: {snapshot.relative}: {type(exc).__name__}",
                    )
                if metadata is None:
                    if snapshot.existed:
                        return _RollbackResult(False, f"已有文件已消失: {snapshot.relative}")
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    return _RollbackResult(False, f"本轮路径不再是普通文件: {snapshot.relative}")
                try:
                    digest, size, _, _ = self._artifact_fingerprint(
                        repository_files,
                        snapshot.relative,
                        approved=snapshot.approved_leaf,
                    )
                except ValueError as exc:
                    return _RollbackResult(False, str(exc))
                if (digest, size) != (snapshot.written_digest, snapshot.written_size):
                    return _RollbackResult(
                        False,
                        f"本轮路径内容已被其他操作修改: {snapshot.relative}",
                    )

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
                            repository_files,
                            snapshot.approved_leaf,
                            snapshot.original_content,
                            snapshot.original_mode,
                        )
                    else:
                        self._unlink_rollback_file(
                            repository_files,
                            snapshot.approved_leaf,
                        )
            except OSError as exc:
                return _RollbackResult(False, f"恢复本轮路径失败: {type(exc).__name__}")

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

    def _unlink_rollback_file(
        self,
        repository_files: _RepositoryFiles,
        approved: _RepositoryLeaf,
    ) -> None:
        repository_files.unlink_approved(approved)

    def _atomic_restore_file(
        self,
        repository_files: _RepositoryFiles,
        approved: _RepositoryLeaf,
        content: bytes,
        mode: int,
    ) -> None:
        repository_files.atomic_restore(approved, content, mode)

    @staticmethod
    def _verified_tree_entries(
        result: _CommandResult,
    ) -> dict[str, tuple[str, str]] | None:
        if not result.ok or not result.stdout or not result.stdout.endswith("\0"):
            return None
        entries: dict[str, tuple[str, str]] = {}
        for record in result.stdout[:-1].split("\0"):
            try:
                header, path = record.split("\t", 1)
            except ValueError:
                return None
            fields = header.split()
            if (
                len(fields) != 3
                or fields[1] != "blob"
                or fields[0] not in {"100644", "100755"}
                or re.fullmatch(r"[0-9a-fA-F]{40,64}", fields[2]) is None
                or not path
                or path in entries
            ):
                return None
            entries[path] = (fields[0], fields[2])
        return entries

    async def _git_blob_matches(
        self,
        *,
        repo: Path,
        object_id: str,
        expected_digest: str,
        expected_size: int,
        timeout_seconds: int,
    ) -> bool:
        """Stream one committed blob into a bounded SHA-256 comparison."""

        env = {key: value for key, value in os.environ.items() if key in _SUBPROCESS_ENV_ALLOWLIST}
        env.update(
            {
                "GIT_ALLOW_PROTOCOL": "file:https:ssh",
                "GIT_GRAFT_FILE": os.devnull,
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "NO_COLOR": "1",
            }
        )
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                "cat-file",
                "blob",
                object_id,
                cwd=str(repo),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=os.name == "posix",
            )
        except (FileNotFoundError, PermissionError, OSError):
            return False

        async def consume() -> tuple[str, int] | None:
            assert process.stdout is not None
            digest = hashlib.sha256()
            size = 0
            while chunk := await process.stdout.read(64 * 1024):
                size += len(chunk)
                if size > expected_size:
                    return None
                digest.update(chunk)
            return digest.hexdigest(), size

        try:
            fingerprint = await asyncio.wait_for(
                consume(),
                timeout=float(timeout_seconds),
            )
            if fingerprint is None:
                await self._stop_process(process)
                return False
            try:
                await asyncio.wait_for(process.wait(), timeout=float(timeout_seconds))
            except TimeoutError:
                await self._stop_process(process)
                return False
        except asyncio.CancelledError:
            await self._stop_process(process)
            raise
        except Exception:
            await self._stop_process(process)
            return False
        return process.returncode == 0 and fingerprint == (expected_digest, expected_size)

    async def _committed_artifacts_match(
        self,
        *,
        repo: Path,
        commit_sha: str,
        sealed_paths: list[_RollbackPath],
        timeout_seconds: int,
    ) -> bool:
        relative_paths = [snapshot.relative for snapshot in sealed_paths]
        tree = await self._run_argv(
            [
                "git",
                "ls-tree",
                "--full-tree",
                "-rz",
                commit_sha,
                "--",
                *relative_paths,
            ],
            cwd=repo,
            timeout_seconds=timeout_seconds,
        )
        entries = self._verified_tree_entries(tree)
        if entries is None or set(entries) != set(relative_paths):
            return False
        for snapshot in sealed_paths:
            if (
                snapshot.written_digest is None
                or snapshot.written_size is None
                or snapshot.written_git_mode is None
            ):
                return False
            mode, object_id = entries[snapshot.relative]
            if mode != snapshot.written_git_mode:
                return False
            if not await self._git_blob_matches(
                repo=repo,
                object_id=object_id,
                expected_digest=snapshot.written_digest,
                expected_size=snapshot.written_size,
                timeout_seconds=timeout_seconds,
            ):
                return False
        return True

    async def _git_publish(
        self,
        *,
        repo: Path,
        paths: list[Path],
        slug: str,
        title: str,
        push_target: str,
        branch: str,
        preflight_head: str,
        sealed_paths: list[_RollbackPath],
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

        if not await self._committed_artifacts_match(
            repo=repo,
            commit_sha=commit_sha,
            sealed_paths=sealed_paths,
            timeout_seconds=timeout,
        ):
            return _GitPublishResult(
                success=False,
                commit_sha=commit_sha,
                error="本地 commit 内容或文件模式与批准产物不一致，拒绝 push",
            )

        ancestry = await self._run_argv(
            ["git", "rev-list", "--parents", "-n", "1", commit_sha],
            cwd=repo,
            timeout_seconds=timeout,
        )
        if not self._commit_has_exact_parent(
            ancestry,
            commit_sha=commit_sha,
            expected_parent=preflight_head,
        ):
            return _GitPublishResult(
                success=False,
                commit_sha=commit_sha,
                error="本地 commit 的父提交不是发布前 HEAD，拒绝 push",
            )

        current_head_result = await self._run_argv(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=repo,
            timeout_seconds=timeout,
        )
        current_head = self._verified_sha(current_head_result)
        if current_head is None or current_head.lower() != commit_sha.lower():
            return _GitPublishResult(
                success=False,
                commit_sha=commit_sha,
                error="本地 HEAD 在 push 前发生变化，拒绝 push",
            )

        try:
            transport_pin, transport_target = self._transport_url_pin(push_target)
            push = await self._run_argv(
                [
                    "git",
                    *transport_pin,
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "-c",
                    "push.pushOption=",
                    "push",
                    "--porcelain",
                    "--no-follow-tags",
                    "--no-recurse-submodules",
                    "--no-signed",
                    f"--force-with-lease=refs/heads/{branch}:{preflight_head}",
                    "--",
                    transport_target,
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
            transport_pin, transport_target = self._transport_url_pin(push_target)
            verification = await self._run_argv(
                [
                    "git",
                    *transport_pin,
                    "ls-remote",
                    "--exit-code",
                    "--",
                    transport_target,
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

        remote_sha = self._verified_remote_sha(verification, branch)
        if remote_sha is not None and remote_sha.lower() == commit_sha.lower():
            return _GitPublishResult(success=True, commit_sha=commit_sha)

        return _GitPublishResult(
            success=False,
            commit_sha=commit_sha,
            error="git push 已启动，但远端 commit 无法唯一归因于本次发布",
            outcome_uncertain=True,
        )

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
        env = {key: value for key, value in os.environ.items() if key in _SUBPROCESS_ENV_ALLOWLIST}
        env.update(
            {
                "GIT_ALLOW_PROTOCOL": "file:https:ssh",
                "GIT_GRAFT_FILE": os.devnull,
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "NO_COLOR": "1",
            }
        )
        configured_gh = str(getattr(settings, "github_pages_gh_bin", "gh"))
        approved_gh = getattr(self, "_github_pages_gh_binary_path", None)
        if argv and argv[0] in {configured_gh, approved_gh}:
            binary = argv[0]
            repository = str(getattr(settings, "github_pages_repository", ""))
            is_version_probe = argv == [binary, "--version"]
            is_api_call = approved_gh_api_argv(
                argv,
                repository=repository,
                binary=binary,
            )
            if not is_version_probe and not is_api_call:
                return _CommandResult(started=False, error="unapproved_gh_command")
            if is_api_call and (approved_gh is None or binary != approved_gh):
                return _CommandResult(started=False, error="unapproved_gh_binary")
            if is_api_call:
                approved_digest = getattr(self, "_github_pages_gh_binary_digest", None)
                current_digest = await asyncio.to_thread(
                    _verified_executable_digest,
                    Path(binary),
                )
                if not isinstance(approved_digest, str) or current_digest != approved_digest:
                    return _CommandResult(started=False, error="unapproved_gh_binary")
            for ambient_transport in (
                "ALL_PROXY",
                "CURL_CA_BUNDLE",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
                "REQUESTS_CA_BUNDLE",
                "GIT_SSH",
                "GIT_SSH_COMMAND",
                "SSH_AUTH_SOCK",
                "SSL_CERT_DIR",
                "SSL_CERT_FILE",
            ):
                env.pop(ambient_transport, None)
            secret = getattr(settings, "github_pages_gh_token", "")
            reveal = getattr(secret, "get_secret_value", None)
            token = reveal() if callable(reveal) else str(secret)
            env.update(
                {
                    "DO_NOT_TRACK": "true",
                    "GH_PROMPT_DISABLED": "1",
                    "GH_NO_UPDATE_NOTIFIER": "1",
                    "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
                    "GH_SPINNER_DISABLED": "1",
                    "GH_PAGER": "cat",
                    "GH_TELEMETRY": "false",
                }
            )
            if is_api_call and token.strip():
                env["GH_TOKEN"] = token
            # Isolate credentials, telemetry state, caches, and any incidental
            # files from the daemon user's real home. The project token above
            # is the command's only authentication source.
            with tempfile.TemporaryDirectory(prefix="ai-ops-gh-runtime-") as gh_root:
                root = Path(gh_root)
                for directory in (
                    root / "gh-config",
                    root / "home",
                    root / "tmp",
                    root / "xdg-cache",
                    root / "xdg-config",
                    root / "xdg-data",
                    root / "xdg-state",
                ):
                    directory.mkdir()
                env.update(
                    {
                        "GH_CONFIG_DIR": str(root / "gh-config"),
                        "HOME": str(root / "home"),
                        "TMP": str(root / "tmp"),
                        "TEMP": str(root / "tmp"),
                        "TMPDIR": str(root / "tmp"),
                        "USERPROFILE": str(root / "home"),
                        "XDG_CACHE_HOME": str(root / "xdg-cache"),
                        "XDG_CONFIG_HOME": str(root / "xdg-config"),
                        "XDG_DATA_HOME": str(root / "xdg-data"),
                        "XDG_STATE_HOME": str(root / "xdg-state"),
                    }
                )
                return await self._execute_argv(
                    argv,
                    cwd=cwd,
                    timeout_seconds=timeout_seconds,
                    env=env,
                )
        return await self._execute_argv(
            argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            env=env,
        )

    async def _execute_argv(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        env: dict[str, str],
    ) -> _CommandResult:
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
