"""Fail-closed asset imports for the Agent control-plane boundary.

The public Agent contract accepts paths from an untrusted caller.  Publishers,
plans, and approvals must not keep referring to those mutable source paths.  An
asset is therefore copied once into a private, content-addressed vault and is
verified again before a side effect uses it.

This module deliberately has no database, API, or Settings dependency.  A
caller must supply both roots explicitly, which makes the same primitive usable
from HTTP services, workers, migrations, and offline tests.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
from typing import BinaryIO


DEFAULT_MAX_ASSET_BYTES = 256 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_STORAGE_SUFFIX_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o400
_TEMP_FILE_ATTEMPTS = 128

# Capture the interpreter/platform feature set before tests or callers replace
# an ``os`` function.  Agent imports rely on the real POSIX *at primitives;
# falling back to a path-only check would reintroduce symlink-swap races.
_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_STAT_SUPPORTS_DIR_FD = os.stat in os.supports_dir_fd
_STAT_SUPPORTS_NOFOLLOW = os.stat in os.supports_follow_symlinks
_MKDIR_SUPPORTS_DIR_FD = os.mkdir in os.supports_dir_fd
_UNLINK_SUPPORTS_DIR_FD = os.unlink in os.supports_dir_fd
_LINK_SUPPORTS_DIR_FD = os.link in os.supports_dir_fd
_LINK_SUPPORTS_NOFOLLOW = os.link in os.supports_follow_symlinks


class AssetVaultError(Exception):
    """Base class for safe, caller-visible asset-vault failures.

    ``code`` is suitable for a stable API error envelope.  Error messages are
    intentionally path-free: logs may attach an internal correlation ID, but
    must not echo the untrusted source path to an Agent caller.
    """

    code = "asset_vault_error"


class AssetVaultConfigurationError(AssetVaultError):
    code = "asset_vault_configuration_invalid"


class AssetSourceRejectedError(AssetVaultError):
    code = "asset_source_rejected"


class AssetTooLargeError(AssetVaultError):
    code = "asset_too_large"


class AssetVaultStorageError(AssetVaultError):
    code = "asset_vault_storage_failed"


class AssetIntegrityError(AssetVaultError):
    code = "asset_integrity_failed"


@dataclass(frozen=True, slots=True)
class VaultedAsset:
    """Immutable identity returned after a successful vault import."""

    sha256: str
    size_bytes: int
    vault_path: Path


@dataclass(frozen=True, slots=True)
class OpenedVaultedAsset:
    """A verified vault inode kept open and rewound for trusted streaming."""

    sha256: str
    size_bytes: int
    vault_path: Path
    handle: BinaryIO

    def close(self) -> None:
        self.handle.close()


class _UnsafePathError(Exception):
    """Private sentinel whose message is never exposed outside this module."""


def _current_uid() -> int:
    try:
        return os.getuid()
    except (AttributeError, OSError):
        raise AssetVaultConfigurationError(
            "secure asset filesystem primitives are unavailable"
        ) from None


def _supports_secure_dir_fd() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_CLOEXEC")
        and hasattr(os, "fchmod")
        and hasattr(os, "getuid")
        and _OPEN_SUPPORTS_DIR_FD
        and _STAT_SUPPORTS_DIR_FD
        and _STAT_SUPPORTS_NOFOLLOW
        and _MKDIR_SUPPORTS_DIR_FD
        and _UNLINK_SUPPORTS_DIR_FD
        and _LINK_SUPPORTS_DIR_FD
        and _LINK_SUPPORTS_NOFOLLOW
    )


def _require_secure_dir_fd() -> None:
    if not _supports_secure_dir_fd():
        raise AssetVaultConfigurationError("secure asset filesystem primitives are unavailable")


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _enforce_private_directory_fd(
    directory_fd: int,
    *,
    storage_error: bool,
) -> os.stat_result:
    """Make one owned POSIX directory exactly 0700 and verify the result."""

    error_type = AssetVaultStorageError if storage_error else AssetVaultConfigurationError
    error_message = (
        "asset vault destination is unavailable"
        if storage_error
        else "asset vault root is unavailable"
    )
    try:
        before = os.fstat(directory_fd)
        if not stat.S_ISDIR(before.st_mode) or before.st_uid != _current_uid():
            raise _UnsafePathError
        if stat.S_IMODE(before.st_mode) != _PRIVATE_DIRECTORY_MODE:
            os.fchmod(directory_fd, _PRIVATE_DIRECTORY_MODE)
        after = os.fstat(directory_fd)
        if (
            not _same_inode(before, after)
            or not stat.S_ISDIR(after.st_mode)
            or after.st_uid != _current_uid()
            or stat.S_IMODE(after.st_mode) != _PRIVATE_DIRECTORY_MODE
        ):
            raise _UnsafePathError
        return after
    except AssetVaultConfigurationError:
        raise
    except (OSError, ValueError, _UnsafePathError):
        raise error_type(error_message) from None


def _open_private_directory_path(path: Path) -> tuple[int, os.stat_result]:
    """Open a vault directory without following its final component."""

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory_fd: int | None = None
    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise _UnsafePathError
        directory_fd = os.open(path, flags)
        opened = _enforce_private_directory_fd(directory_fd, storage_error=False)
        after = os.lstat(path)
        if not _same_inode(before, opened) or not _same_inode(opened, after):
            raise _UnsafePathError
        return directory_fd, opened
    except AssetVaultError:
        if directory_fd is not None:
            os.close(directory_fd)
        raise
    except (OSError, ValueError, _UnsafePathError):
        if directory_fd is not None:
            try:
                os.close(directory_fd)
            except OSError:
                pass
        raise AssetVaultConfigurationError("asset vault root is unavailable") from None


def _validated_max_bytes(max_bytes: int) -> int:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise AssetVaultConfigurationError("asset size limit is invalid")
    return max_bytes


def _validated_root(
    value: str | os.PathLike[str],
    *,
    create: bool,
    purpose: str,
) -> Path:
    _require_secure_dir_fd()
    try:
        raw = Path(value).expanduser()
        if raw.is_symlink():
            raise _UnsafePathError
        if create:
            raw.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        resolved = raw.resolve(strict=True)
        mode = os.lstat(resolved).st_mode
        if not stat.S_ISDIR(mode):
            raise _UnsafePathError
        if raw.is_symlink():
            raise _UnsafePathError
        if purpose == "vault":
            directory_fd, _ = _open_private_directory_path(resolved)
            os.close(directory_fd)
        return resolved
    except AssetVaultError:
        raise
    except (OSError, RuntimeError, TypeError, _UnsafePathError):
        if purpose == "import":
            raise AssetVaultConfigurationError("asset import root is unavailable") from None
        raise AssetVaultConfigurationError("asset vault root is unavailable") from None


def _source_relative_path(
    source: str | os.PathLike[str],
    import_root: Path,
) -> tuple[Path, Path]:
    try:
        supplied = Path(source)
    except (OSError, TypeError, ValueError):
        raise AssetSourceRejectedError("asset source is not allowed") from None
    if ".." in supplied.parts:
        raise AssetSourceRejectedError("asset source is not allowed")

    candidate = supplied if supplied.is_absolute() else import_root / supplied
    try:
        # The strict canonical resolution is an explicit authorization step,
        # not merely a convenience existence check.
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(import_root)

        # Keep the lexical path for no-follow traversal.  A symlink that points
        # back inside import_root passes the canonical boundary check but must
        # still be rejected as a symlink.
        lexical = Path(os.path.abspath(os.path.normpath(os.fspath(candidate))))
        relative = lexical.relative_to(import_root)
        if not relative.parts:
            raise _UnsafePathError
        return resolved, relative
    except (OSError, RuntimeError, ValueError, _UnsafePathError):
        raise AssetSourceRejectedError("asset source is not allowed") from None


def _open_regular_beneath(root: Path, relative: Path) -> tuple[int, os.stat_result]:
    """Open a regular file without following any path-component symlink."""

    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise _UnsafePathError

    _require_secure_dir_fd()

    directory_fds: list[int] = []
    file_fd: int | None = None
    try:
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        current_fd = os.open(root, directory_flags)
        directory_fds.append(current_fd)

        for part in relative.parts[:-1]:
            entry = os.stat(part, dir_fd=current_fd, follow_symlinks=False)
            if not stat.S_ISDIR(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
                raise _UnsafePathError
            current_fd = os.open(part, directory_flags, dir_fd=current_fd)
            directory_fds.append(current_fd)

        name = relative.parts[-1]
        before = os.stat(name, dir_fd=current_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise _UnsafePathError

        file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        # Prevent a swapped-in FIFO from blocking between stat and open.
        file_flags |= getattr(os, "O_NONBLOCK", 0)
        file_fd = os.open(name, file_flags, dir_fd=current_fd)
        after = os.fstat(file_fd)
        identity_changed = (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        if identity_changed or not stat.S_ISREG(after.st_mode):
            os.close(file_fd)
            file_fd = None
            raise _UnsafePathError
        opened_fd = file_fd
        file_fd = None
        return opened_fd, after
    except AssetVaultError:
        raise
    except (OSError, ValueError):
        raise _UnsafePathError from None
    finally:
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
        for directory_fd in reversed(directory_fds):
            try:
                os.close(directory_fd)
            except OSError:
                pass


def _same_open_file(before: os.stat_result, after: os.stat_result, total: int) -> bool:
    stable_fields = (
        before.st_dev == after.st_dev,
        before.st_ino == after.st_ino,
        before.st_size == after.st_size == total,
        before.st_mtime_ns == after.st_mtime_ns,
        before.st_ctime_ns == after.st_ctime_ns,
        stat.S_ISREG(after.st_mode),
    )
    return all(stable_fields)


def inspect_import_asset_size(
    source: str | os.PathLike[str],
    *,
    import_root: str | os.PathLike[str],
    max_bytes: int = DEFAULT_MAX_ASSET_BYTES,
) -> int:
    """Securely preflight one import source and return its current byte size.

    This helper is intentionally read-only: it does not create a vault or hash
    the source.  It is suitable for rejecting an aggregate request before any
    permanent imports start.  The subsequent import must still enforce both
    the per-file and aggregate remaining quota because the source may change
    after this inspection.
    """

    limit = _validated_max_bytes(max_bytes)
    authorized_root = _validated_root(import_root, create=False, purpose="import")
    _, source_relative = _source_relative_path(source, authorized_root)
    try:
        source_fd, source_stat = _open_regular_beneath(authorized_root, source_relative)
    except _UnsafePathError:
        raise AssetSourceRejectedError("asset source must be a non-symlink regular file") from None
    try:
        if source_stat.st_size > limit:
            raise AssetTooLargeError("asset exceeds the configured size limit")
        after = os.fstat(source_fd)
        if not _same_inode(source_stat, after) or not stat.S_ISREG(after.st_mode):
            raise AssetSourceRejectedError("asset source changed during inspection")
        return after.st_size
    except AssetVaultError:
        raise
    except OSError:
        raise AssetSourceRejectedError("asset source could not be inspected") from None
    finally:
        try:
            os.close(source_fd)
        except OSError:
            pass


def _write_temp_all(temp_fd: int, chunk: bytes) -> None:
    remaining = memoryview(chunk)
    while remaining:
        written = os.write(temp_fd, remaining)
        if written <= 0:
            raise OSError
        remaining = remaining[written:]


def _copy_source_to_temp(
    source_fd: int,
    source_stat: os.stat_result,
    temp_fd: int,
    *,
    max_bytes: int,
) -> tuple[str, int]:
    """Copy and hash while leaving both caller-owned descriptors open."""

    digest = hashlib.sha256()
    total = 0
    try:
        while chunk := os.read(source_fd, _COPY_CHUNK_BYTES):
            total += len(chunk)
            if total > max_bytes:
                raise AssetTooLargeError("asset exceeds the configured size limit")
            digest.update(chunk)
            _write_temp_all(temp_fd, chunk)

        after = os.fstat(source_fd)
        if not _same_open_file(source_stat, after, total):
            raise AssetSourceRejectedError("asset source changed during import")
        os.fsync(temp_fd)
        os.fchmod(temp_fd, _PRIVATE_FILE_MODE)
        temp_stat = os.fstat(temp_fd)
        if (
            not stat.S_ISREG(temp_stat.st_mode)
            or temp_stat.st_uid != _current_uid()
            or stat.S_IMODE(temp_stat.st_mode) != _PRIVATE_FILE_MODE
            or temp_stat.st_size != total
        ):
            raise AssetVaultStorageError("asset vault write failed")
    except AssetVaultError:
        raise
    except (OSError, ValueError):
        raise AssetVaultStorageError("asset vault write failed") from None
    return digest.hexdigest(), total


def _safe_storage_suffix(source: Path) -> str:
    suffix = source.suffix.lower()
    return suffix if _SAFE_STORAGE_SUFFIX_RE.fullmatch(suffix) is not None else ""


def _digest_path(vault_root: Path, sha256: str, suffix: str = "") -> Path:
    return vault_root / "sha256" / sha256[:2] / sha256[2:4] / f"{sha256}{suffix}"


def _open_private_child_directory(
    parent_fd: int,
    name: str,
    *,
    create: bool,
) -> tuple[int, os.stat_result]:
    child_fd: int | None = None
    try:
        if create:
            try:
                os.mkdir(name, _PRIVATE_DIRECTORY_MODE, dir_fd=parent_fd)
            except FileExistsError:
                pass
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise _UnsafePathError
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        child_fd = os.open(name, flags, dir_fd=parent_fd)
        opened = _enforce_private_directory_fd(child_fd, storage_error=True)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_inode(before, opened) or not _same_inode(opened, after):
            raise _UnsafePathError
        return child_fd, opened
    except AssetVaultError:
        if child_fd is not None:
            try:
                os.close(child_fd)
            except OSError:
                pass
        raise
    except (OSError, _UnsafePathError):
        if child_fd is not None:
            try:
                os.close(child_fd)
            except OSError:
                pass
        raise AssetVaultStorageError("asset vault destination is unavailable") from None


def _ensure_digest_directory(
    vault_root_fd: int,
    vault_root: Path,
    sha256: str,
) -> tuple[Path, int]:
    current_path = vault_root
    try:
        current_fd = os.dup(vault_root_fd)
    except OSError:
        raise AssetVaultStorageError("asset vault destination is unavailable") from None
    try:
        for part in ("sha256", sha256[:2], sha256[2:4]):
            next_fd, _ = _open_private_child_directory(current_fd, part, create=True)
            os.close(current_fd)
            current_fd = next_fd
            current_path /= part
        path_stat = os.lstat(current_path)
        opened_stat = os.fstat(current_fd)
        if (
            stat.S_ISLNK(path_stat.st_mode)
            or not _same_inode(path_stat, opened_stat)
            or stat.S_IMODE(opened_stat.st_mode) != _PRIVATE_DIRECTORY_MODE
            or opened_stat.st_uid != _current_uid()
        ):
            raise _UnsafePathError
        return current_path, current_fd
    except AssetVaultError:
        try:
            os.close(current_fd)
        except OSError:
            pass
        raise
    except (OSError, ValueError, _UnsafePathError):
        try:
            os.close(current_fd)
        except OSError:
            pass
        raise AssetVaultStorageError("asset vault destination is unavailable") from None


def _validate_existing_vault_directory_chain(
    vault_root: Path,
    directory_parts: tuple[str, ...],
) -> None:
    current_fd: int | None = None
    try:
        current_fd, _ = _open_private_directory_path(vault_root)
        for part in directory_parts:
            next_fd, _ = _open_private_child_directory(current_fd, part, create=False)
            os.close(current_fd)
            current_fd = next_fd
    finally:
        if current_fd is not None:
            try:
                os.close(current_fd)
            except OSError:
                pass


def _create_private_temp_file(vault_root_fd: int) -> tuple[int, str, os.stat_result]:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    for _ in range(_TEMP_FILE_ATTEMPTS):
        name = f".asset-{secrets.token_hex(16)}.tmp"
        try:
            temp_fd = os.open(
                name,
                flags,
                _PRIVATE_FILE_MODE,
                dir_fd=vault_root_fd,
            )
        except FileExistsError:
            continue
        except OSError:
            raise AssetVaultStorageError("asset vault write failed") from None
        try:
            os.fchmod(temp_fd, _PRIVATE_FILE_MODE)
            opened = os.fstat(temp_fd)
            entry = os.stat(name, dir_fd=vault_root_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != _current_uid()
                or stat.S_IMODE(opened.st_mode) != _PRIVATE_FILE_MODE
                or not _same_inode(opened, entry)
            ):
                raise _UnsafePathError
            return temp_fd, name, opened
        except (OSError, ValueError, _UnsafePathError):
            try:
                os.close(temp_fd)
            except OSError:
                pass
            try:
                os.unlink(name, dir_fd=vault_root_fd)
            except OSError:
                pass
            raise AssetVaultStorageError("asset vault write failed") from None
    raise AssetVaultStorageError("asset vault temporary namespace is exhausted")


def _entry_stat(directory_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _remove_entry_if_unchanged(
    directory_fd: int,
    name: str,
    expected: os.stat_result,
) -> bool:
    """Best-effort unlink, declining an entry already replaced when inspected."""

    try:
        current = _entry_stat(directory_fd, name)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not _same_inode(current, expected):
        return False
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _remove_current_entry(directory_fd: int, name: str) -> None:
    """Remove the current entry in a private, randomized temp namespace."""

    try:
        current = _entry_stat(directory_fd, name)
    except OSError:
        return
    _remove_entry_if_unchanged(directory_fd, name, current)


def _verify_temp_entry(
    vault_root_fd: int,
    temp_name: str,
    temp_fd: int,
    *,
    expected_size: int,
) -> os.stat_result:
    try:
        opened = os.fstat(temp_fd)
        entry = _entry_stat(vault_root_fd, temp_name)
        if (
            not _same_inode(opened, entry)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != _current_uid()
            or stat.S_IMODE(opened.st_mode) != _PRIVATE_FILE_MODE
            or opened.st_size != expected_size
        ):
            raise _UnsafePathError
        return opened
    except (OSError, ValueError, _UnsafePathError):
        raise AssetVaultStorageError("asset vault temporary file changed") from None


def _open_regular_at(
    directory_fd: int,
    name: str,
) -> tuple[int, os.stat_result]:
    file_fd: int | None = None
    try:
        before = _entry_stat(directory_fd, name)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise _UnsafePathError
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NONBLOCK", 0)
        file_fd = os.open(name, flags, dir_fd=directory_fd)
        opened = os.fstat(file_fd)
        if not _same_inode(before, opened) or not stat.S_ISREG(opened.st_mode):
            raise _UnsafePathError
        return file_fd, opened
    except (OSError, ValueError, _UnsafePathError):
        if file_fd is not None:
            try:
                os.close(file_fd)
            except OSError:
                pass
        raise AssetVaultStorageError("asset vault commit verification failed") from None


def _verify_newly_linked_destination(
    *,
    destination_dir_fd: int,
    destination_name: str,
    destination_path: Path,
    linked_stat: os.stat_result,
    temp_fd: int,
    temp_stat: os.stat_result,
    expected_sha256: str,
    expected_size: int,
    max_bytes: int,
) -> None:
    destination_fd: int | None = None
    try:
        if not _same_inode(linked_stat, temp_stat) or not stat.S_ISREG(linked_stat.st_mode):
            raise _UnsafePathError
        destination_fd, opened = _open_regular_at(destination_dir_fd, destination_name)
        if not _same_inode(opened, temp_stat):
            raise _UnsafePathError

        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(destination_fd, _COPY_CHUNK_BYTES):
            total += len(chunk)
            if total > max_bytes:
                raise AssetTooLargeError("asset exceeds the configured size limit")
            digest.update(chunk)

        after = os.fstat(destination_fd)
        path_after = _entry_stat(destination_dir_fd, destination_name)
        absolute_after = os.lstat(destination_path)
        temp_after = os.fstat(temp_fd)
        if (
            not _same_open_file(opened, after, total)
            or not _same_inode(after, path_after)
            or not _same_inode(path_after, absolute_after)
            or not _same_inode(after, temp_after)
            or total != expected_size
            or digest.hexdigest() != expected_sha256
        ):
            raise _UnsafePathError
    except AssetVaultError:
        raise
    except (OSError, ValueError, _UnsafePathError):
        raise AssetVaultStorageError("asset vault commit verification failed") from None
    finally:
        if destination_fd is not None:
            try:
                os.close(destination_fd)
            except OSError:
                pass


def _directory_path_matches_fd(path: Path, directory_fd: int) -> bool:
    try:
        entry = os.lstat(path)
        opened = os.fstat(directory_fd)
    except OSError:
        return False
    return (
        not stat.S_ISLNK(entry.st_mode)
        and stat.S_ISDIR(opened.st_mode)
        and _same_inode(entry, opened)
        and opened.st_uid == _current_uid()
        and stat.S_IMODE(opened.st_mode) == _PRIVATE_DIRECTORY_MODE
    )


def _fsync_directory_fd(directory_fd: int) -> None:
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        unsupported = {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}
        if exc.errno not in unsupported:
            raise AssetVaultStorageError("asset vault directory sync failed") from None


def import_asset_to_vault(
    source: str | os.PathLike[str],
    *,
    import_root: str | os.PathLike[str],
    vault_root: str | os.PathLike[str],
    max_bytes: int = DEFAULT_MAX_ASSET_BYTES,
) -> VaultedAsset:
    """Copy one authorized regular file into an immutable SHA-256 path.

    Relative inputs are interpreted below ``import_root``.  Absolute inputs are
    accepted only when their canonical path is below the same root.  Every path
    component is opened without following symlinks, and a second importer can
    only reuse (never overwrite) an already verified digest path.
    """

    _require_secure_dir_fd()
    limit = _validated_max_bytes(max_bytes)
    authorized_root = _validated_root(import_root, create=False, purpose="import")
    destination_root = _validated_root(vault_root, create=True, purpose="vault")
    if (
        authorized_root == destination_root
        or authorized_root.is_relative_to(destination_root)
        or destination_root.is_relative_to(authorized_root)
    ):
        raise AssetVaultConfigurationError("asset import and vault roots must not overlap")
    _, source_relative = _source_relative_path(source, authorized_root)
    storage_suffix = _safe_storage_suffix(source_relative)

    try:
        source_fd, source_stat = _open_regular_beneath(authorized_root, source_relative)
    except _UnsafePathError:
        raise AssetSourceRejectedError("asset source must be a non-symlink regular file") from None
    if source_stat.st_size > limit:
        os.close(source_fd)
        raise AssetTooLargeError("asset exceeds the configured size limit")

    vault_root_fd: int | None = None
    temp_fd: int | None = None
    temp_name: str | None = None
    destination_dir_fd: int | None = None
    destination_name: str | None = None
    linked_stat: os.stat_result | None = None
    temp_stat: os.stat_result | None = None
    created_destination = False
    destination_verified = False
    commit_succeeded = False
    try:
        vault_root_fd, _ = _open_private_directory_path(destination_root)
        temp_fd, temp_name, _ = _create_private_temp_file(vault_root_fd)
        sha256, size_bytes = _copy_source_to_temp(
            source_fd,
            source_stat,
            temp_fd,
            max_bytes=limit,
        )
        os.close(source_fd)
        source_fd = -1
        temp_stat = _verify_temp_entry(
            vault_root_fd,
            temp_name,
            temp_fd,
            expected_size=size_bytes,
        )

        destination_dir, destination_dir_fd = _ensure_digest_directory(
            vault_root_fd,
            destination_root,
            sha256,
        )
        destination = _digest_path(destination_root, sha256, storage_suffix)
        destination_name = destination.name
        record = VaultedAsset(
            sha256=sha256,
            size_bytes=size_bytes,
            vault_path=destination,
        )
        try:
            # A hard link publishes a fully fsynced file atomically and fails
            # if the digest path already exists.  We never replace an existing
            # content identity.
            os.link(
                temp_name,
                destination_name,
                src_dir_fd=vault_root_fd,
                dst_dir_fd=destination_dir_fd,
                follow_symlinks=False,
            )
            created_destination = True
        except FileExistsError:
            verify_vaulted_asset(record, vault_root=destination_root, max_bytes=limit)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                verify_vaulted_asset(record, vault_root=destination_root, max_bytes=limit)
            else:
                raise AssetVaultStorageError("asset vault commit failed") from None
        except (TypeError, ValueError):
            raise AssetVaultStorageError("asset vault commit failed") from None

        if created_destination:
            try:
                linked_stat = _entry_stat(destination_dir_fd, destination_name)
            except OSError:
                raise AssetVaultStorageError("asset vault commit verification failed") from None
            _verify_newly_linked_destination(
                destination_dir_fd=destination_dir_fd,
                destination_name=destination_name,
                destination_path=destination,
                linked_stat=linked_stat,
                temp_fd=temp_fd,
                temp_stat=temp_stat,
                expected_sha256=sha256,
                expected_size=size_bytes,
                max_bytes=limit,
            )
            destination_verified = True

        if not _directory_path_matches_fd(destination_root, vault_root_fd) or not (
            _directory_path_matches_fd(destination_dir, destination_dir_fd)
        ):
            raise AssetVaultStorageError("asset vault path changed during commit")
        if not _remove_entry_if_unchanged(vault_root_fd, temp_name, temp_stat):
            raise AssetVaultStorageError("asset vault temporary cleanup failed")
        temp_name = None
        _fsync_directory_fd(destination_dir_fd)
        _fsync_directory_fd(vault_root_fd)
        commit_succeeded = True
        return record
    except AssetVaultError:
        raise
    except (OSError, ValueError):
        # Never include path-bearing OS error text in the caller-visible
        # exception.
        raise AssetVaultStorageError("asset vault write failed") from None
    finally:
        cleanup_destination_stat = linked_stat or temp_stat
        if (
            created_destination
            and not commit_succeeded
            and not destination_verified
            and cleanup_destination_stat is not None
            and destination_dir_fd is not None
            and destination_name is not None
        ):
            _remove_entry_if_unchanged(
                destination_dir_fd,
                destination_name,
                cleanup_destination_stat,
            )
        if source_fd >= 0:
            try:
                os.close(source_fd)
            except OSError:
                pass
        if vault_root_fd is not None and temp_name is not None:
            _remove_current_entry(vault_root_fd, temp_name)
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if destination_dir_fd is not None:
            try:
                os.close(destination_dir_fd)
            except OSError:
                pass
        if vault_root_fd is not None:
            try:
                os.close(vault_root_fd)
            except OSError:
                pass


def _validated_record(asset: VaultedAsset, *, max_bytes: int) -> tuple[str, int, Path]:
    sha256 = asset.sha256
    size_bytes = asset.size_bytes
    if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
        raise AssetIntegrityError("vaulted asset identity is invalid")
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise AssetIntegrityError("vaulted asset size is invalid")
    if size_bytes > max_bytes:
        raise AssetTooLargeError("asset exceeds the configured size limit")
    try:
        vault_path = Path(asset.vault_path)
    except TypeError:
        raise AssetIntegrityError("vaulted asset path is invalid") from None
    return sha256, size_bytes, vault_path


@dataclass(frozen=True, slots=True)
class _OpenedVaultedAssetFD:
    sha256: str
    expected_size: int
    vault_path: Path
    file_fd: int
    opened_stat: os.stat_result


def _open_vaulted_asset_fd(
    asset: VaultedAsset,
    *,
    vault_root: str | os.PathLike[str],
    max_bytes: int,
) -> _OpenedVaultedAssetFD:
    """Authorize and no-follow open one canonical vault inode without reading it."""

    limit = _validated_max_bytes(max_bytes)
    destination_root = _validated_root(vault_root, create=False, purpose="vault")
    sha256, expected_size, supplied_path = _validated_record(asset, max_bytes=limit)
    supplied_name = supplied_path.name
    if supplied_name == sha256:
        storage_suffix = ""
    elif supplied_name.startswith(sha256):
        storage_suffix = supplied_name[len(sha256) :]
        if _SAFE_STORAGE_SUFFIX_RE.fullmatch(storage_suffix) is None:
            raise AssetIntegrityError("vaulted asset path is invalid")
    else:
        raise AssetIntegrityError("vaulted asset path is invalid")
    expected_path = _digest_path(destination_root, sha256, storage_suffix)

    # Requiring the exact content-addressed location prevents a valid digest
    # from authorizing an arbitrary second file elsewhere below the vault.
    if not supplied_path.is_absolute() or supplied_path != expected_path:
        raise AssetIntegrityError("vaulted asset path is invalid")
    try:
        resolved = supplied_path.resolve(strict=True)
        resolved.relative_to(destination_root)
        if resolved != expected_path:
            raise _UnsafePathError
        relative = expected_path.relative_to(destination_root)
        _validate_existing_vault_directory_chain(
            destination_root,
            tuple(relative.parts[:-1]),
        )
        file_fd, opened_stat = _open_regular_beneath(destination_root, relative)
    except (OSError, RuntimeError, ValueError, _UnsafePathError):
        raise AssetIntegrityError("vaulted asset failed integrity verification") from None
    return _OpenedVaultedAssetFD(
        sha256=sha256,
        expected_size=expected_size,
        vault_path=expected_path,
        file_fd=file_fd,
        opened_stat=opened_stat,
    )


def open_verified_vaulted_asset(
    asset: VaultedAsset,
    *,
    vault_root: str | os.PathLike[str],
    max_bytes: int = DEFAULT_MAX_ASSET_BYTES,
) -> OpenedVaultedAsset:
    """Open, hash, and rewind one vaulted file without re-opening its path.

    The returned handle references the same inode that was opened with
    no-follow checks and hashed.  Callers must close it.  This primitive is for
    response/execution boundaries where validating a path and later reopening
    it would introduce a symlink/inode replacement race.
    """

    limit = _validated_max_bytes(max_bytes)
    opened = _open_vaulted_asset_fd(
        asset,
        vault_root=vault_root,
        max_bytes=limit,
    )

    digest = hashlib.sha256()
    total = 0
    handle: BinaryIO | None = None
    try:
        handle = os.fdopen(opened.file_fd, "rb", closefd=True)
        while chunk := handle.read(_COPY_CHUNK_BYTES):
            total += len(chunk)
            if total > limit:
                raise AssetTooLargeError("asset exceeds the configured size limit")
            digest.update(chunk)
        after = os.fstat(handle.fileno())
        if not _same_open_file(opened.opened_stat, after, total):
            raise AssetIntegrityError("vaulted asset failed integrity verification")
        if total != opened.expected_size or digest.hexdigest() != opened.sha256:
            raise AssetIntegrityError("vaulted asset failed integrity verification")
        handle.seek(0)
    except AssetVaultError:
        if handle is not None:
            handle.close()
        else:
            os.close(opened.file_fd)
        raise
    except (OSError, ValueError):
        if handle is not None:
            handle.close()
        else:
            try:
                os.close(opened.file_fd)
            except OSError:
                pass
        raise AssetIntegrityError("vaulted asset failed integrity verification") from None

    return OpenedVaultedAsset(
        sha256=opened.sha256,
        size_bytes=total,
        vault_path=opened.vault_path,
        handle=handle,
    )


def _write_all(destination_fd: int, chunk: bytes) -> None:
    remaining = memoryview(chunk)
    try:
        while remaining:
            written = os.write(destination_fd, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
    except (OSError, ValueError):
        raise AssetVaultStorageError("verified asset destination write failed") from None


def copy_verified_vaulted_asset(
    asset: VaultedAsset,
    *,
    destination_fd: int,
    vault_root: str | os.PathLike[str],
    max_bytes: int = DEFAULT_MAX_ASSET_BYTES,
) -> VaultedAsset:
    """Secure-open, verify, and copy one vault inode in a single byte pass.

    ``destination_fd`` remains owned by the caller and must identify an empty
    regular file positioned at offset zero. The source is opened with the same
    canonical-path/no-follow checks as :func:`open_verified_vaulted_asset`, but
    each source byte is read exactly once while simultaneously hashing and
    writing the execution copy. Integrity is accepted only after the source
    inode metadata remains stable and the approved size/SHA-256 both match.
    """

    limit = _validated_max_bytes(max_bytes)
    opened = _open_vaulted_asset_fd(
        asset,
        vault_root=vault_root,
        max_bytes=limit,
    )
    try:
        try:
            destination_stat = os.fstat(destination_fd)
            destination_offset = os.lseek(destination_fd, 0, os.SEEK_CUR)
        except (OSError, TypeError, ValueError):
            raise AssetVaultStorageError("verified asset destination is invalid") from None
        if (
            not stat.S_ISREG(destination_stat.st_mode)
            or destination_stat.st_size != 0
            or destination_offset != 0
            or (destination_stat.st_dev, destination_stat.st_ino)
            == (opened.opened_stat.st_dev, opened.opened_stat.st_ino)
        ):
            raise AssetVaultStorageError("verified asset destination is invalid")

        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(opened.file_fd, _COPY_CHUNK_BYTES):
            total += len(chunk)
            if total > limit:
                raise AssetTooLargeError("asset exceeds the configured size limit")
            digest.update(chunk)
            _write_all(destination_fd, chunk)

        after = os.fstat(opened.file_fd)
        if not _same_open_file(opened.opened_stat, after, total):
            raise AssetIntegrityError("vaulted asset failed integrity verification")
        if total != opened.expected_size or digest.hexdigest() != opened.sha256:
            raise AssetIntegrityError("vaulted asset failed integrity verification")
        return VaultedAsset(
            sha256=opened.sha256,
            size_bytes=total,
            vault_path=opened.vault_path,
        )
    except AssetVaultError:
        raise
    except (OSError, TypeError, ValueError):
        raise AssetIntegrityError("vaulted asset failed integrity verification") from None
    finally:
        try:
            os.close(opened.file_fd)
        except OSError:
            pass


def verify_vaulted_asset(
    asset: VaultedAsset,
    *,
    vault_root: str | os.PathLike[str],
    max_bytes: int = DEFAULT_MAX_ASSET_BYTES,
) -> VaultedAsset:
    """Recompute path boundary, byte count, and SHA-256 for a vaulted asset."""

    opened = open_verified_vaulted_asset(
        asset,
        vault_root=vault_root,
        max_bytes=max_bytes,
    )
    try:
        return VaultedAsset(
            sha256=opened.sha256,
            size_bytes=opened.size_bytes,
            vault_path=opened.vault_path,
        )
    finally:
        opened.close()


__all__ = [
    "DEFAULT_MAX_ASSET_BYTES",
    "AssetIntegrityError",
    "AssetSourceRejectedError",
    "AssetTooLargeError",
    "AssetVaultConfigurationError",
    "AssetVaultError",
    "AssetVaultStorageError",
    "OpenedVaultedAsset",
    "VaultedAsset",
    "copy_verified_vaulted_asset",
    "import_asset_to_vault",
    "inspect_import_asset_size",
    "open_verified_vaulted_asset",
    "verify_vaulted_asset",
]
