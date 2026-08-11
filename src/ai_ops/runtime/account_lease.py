"""Cross-process lease for operations sharing one account/profile."""
from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
import stat

from ..config import settings


_POLL_SECONDS = 0.1


class AccountOperationLeaseTimeout(TimeoutError):
    pass


class AccountOperationLease:
    """Kernel-backed exclusive lock scoped to one positive account ID.

    Browser profiles, CLI HOME directories and cookie stores are not safe for a
    health probe and a publish process to mutate/read concurrently.  The lock
    file contains no credential and persists harmlessly between operations; the
    kernel lock itself is released automatically if a process dies.
    """

    def __init__(
        self,
        account_id: int,
        *,
        timeout_seconds: float,
        data_dir: str | Path | None = None,
    ) -> None:
        if account_id <= 0:
            raise ValueError("account_id must be positive")
        self.account_id = account_id
        self.timeout_seconds = max(0.0, float(timeout_seconds))
        effective_data_dir = settings.data_dir if data_dir is None else data_dir
        root = Path(os.path.abspath(Path(effective_data_dir) / "locks" / "accounts"))
        self.path = root / f"account_{account_id}.lock"
        self._fd: int | None = None
        self._locked = False

    def _open(self) -> int:
        root = self.path.parent
        locks_root = root.parent
        if locks_root.is_symlink() or root.is_symlink():
            raise OSError("account lock directory cannot be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if locks_root.is_symlink() or root.is_symlink():
            raise OSError("account lock directory cannot be a symlink")
        for directory in (locks_root, root):
            try:
                directory.chmod(0o700)
            except OSError:
                pass
        if self.path.is_symlink():
            raise OSError("account lock file cannot be a symlink")
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("account lock must be a regular file")
            if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
                raise OSError("account lock owner does not match the process")
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
        raise OSError(f"account locks are unsupported on {os.name}")

    @staticmethod
    def _unlock(fd: int) -> None:
        if os.name == "posix":
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        elif os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    async def __aenter__(self) -> AccountOperationLease:
        self._fd = self._open()
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        try:
            while not self._try_acquire(self._fd):
                if asyncio.get_running_loop().time() >= deadline:
                    raise AccountOperationLeaseTimeout(
                        f"account {self.account_id} operation lease is busy"
                    )
                await asyncio.sleep(_POLL_SECONDS)
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
