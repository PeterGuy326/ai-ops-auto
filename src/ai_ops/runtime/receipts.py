"""Durable, redacted receipts for external publish side effects.

The database transaction that finalizes a PublishJob happens after an external
platform call.  A database outage or process crash in that gap must not erase a
confirmed post identity and tempt an operator to publish it again.  This module
stores a tiny, credential-free sidecar before finalization and lets stale-job
reconciliation recover it.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import secrets

from ..config import settings
from ..core.schemas import PublishResult


logger = logging.getLogger(__name__)

_ACTIVE_RECEIPT_DATA_DIR: ContextVar[str | Path | None] = ContextVar(
    "ai_ops_active_receipt_data_dir",
    default=None,
)

_OPERATION_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SAFE_RAW_KEYS = frozenset(
    {
        "action",
        "actual_privacy",
        "adapter",
        "adapter_version",
        "attempt_id",
        "branch",
        "commit_sha",
        "deploy_id",
        "exit_code",
        "needs_reconciliation",
        "outcome",
        "privacy",
        "publisher_kind",
        "remote_sha",
        "state",
        "write_started",
    }
)
_MAX_RECEIPT_BYTES = 64 * 1024


@contextmanager
def receipt_data_dir_scope(data_dir: str | Path):
    """Route nested adapter journals to one task-local receipt directory."""
    token = _ACTIVE_RECEIPT_DATA_DIR.set(data_dir)
    try:
        yield
    finally:
        _ACTIVE_RECEIPT_DATA_DIR.reset(token)


def new_operation_id() -> str:
    return secrets.token_hex(16)


def _valid_identity(job_id: int, operation_id: str) -> bool:
    return job_id > 0 and bool(_OPERATION_ID_RE.fullmatch(operation_id))


def _receipt_dir(
    job_id: int,
    *,
    create: bool,
    data_dir: str | Path | None = None,
) -> Path:
    effective_data_dir = data_dir
    if effective_data_dir is None:
        effective_data_dir = _ACTIVE_RECEIPT_DATA_DIR.get()
    if effective_data_dir is None:
        effective_data_dir = settings.data_dir
    receipts_root = Path(effective_data_dir).expanduser() / "receipts"
    root = receipts_root / "publish"
    job_dir = root / f"job_{job_id}"
    if receipts_root.is_symlink() or root.is_symlink() or job_dir.is_symlink():
        raise OSError("publish receipt directories cannot be symlinks")
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if receipts_root.is_symlink() or root.is_symlink():
            raise OSError("publish receipt root cannot be a symlink")
        job_dir.mkdir(mode=0o700, exist_ok=True)
        if job_dir.is_symlink():
            raise OSError("publish receipt job directory cannot be a symlink")
        for directory in (receipts_root, root, job_dir):
            try:
                directory.chmod(0o700)
            except OSError:
                pass
    return job_dir


def receipt_path(
    job_id: int,
    operation_id: str,
    *,
    data_dir: str | Path | None = None,
) -> Path:
    if not _valid_identity(job_id, operation_id):
        raise ValueError("invalid publish receipt identity")
    return _receipt_dir(job_id, create=False, data_dir=data_dir) / f"{operation_id}.json"


def _safe_scalar(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:1024]
    return None


def _redacted_raw(raw: dict) -> dict:
    redacted: dict[str, object] = {}
    for key in _SAFE_RAW_KEYS:
        if key not in raw:
            continue
        value = _safe_scalar(raw[key])
        if value is not None:
            redacted[key] = value
    return redacted


def write_publish_receipt(
    *,
    job_id: int | None,
    operation_id: str | None,
    publisher_kind: str,
    result: PublishResult,
    data_dir: str | Path | None = None,
) -> Path | None:
    """Atomically persist a redacted result before the DB finalize transaction.

    Journaling is best effort because a platform may already have accepted the
    write.  A local disk failure cannot make retrying safe, so callers still use
    the in-memory result and normal fail-closed persistence path.
    """
    if job_id is None or operation_id is None or not _valid_identity(job_id, operation_id):
        return None
    try:
        job_dir = _receipt_dir(job_id, create=True, data_dir=data_dir)
        path = job_dir / f"{operation_id}.json"
        payload = {
            "version": 1,
            "job_id": job_id,
            "operation_id": operation_id,
            "publisher_kind": str(publisher_kind)[:64],
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "success": bool(result.success),
            "effect_applied": bool(result.effect_applied),
            "outcome_uncertain": bool(result.outcome_uncertain),
            "platform_post_id": (result.platform_post_id or "")[:128] or None,
            "platform_url": (result.platform_url or "")[:512] or None,
            "raw_response": _redacted_raw(dict(result.raw_response or {})),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > _MAX_RECEIPT_BYTES:
            raise ValueError("redacted publish receipt exceeds size limit")

        temp = job_dir / f".{operation_id}.{secrets.token_hex(8)}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            try:
                dir_fd = os.open(job_dir, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
            return path
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
    except (OSError, TypeError, ValueError):
        logger.exception(
            "could not persist redacted publish receipt",
            extra={"job_id": job_id, "operation_id": operation_id},
        )
        return None


def read_publish_receipt(
    job_id: int,
    operation_id: str,
    *,
    data_dir: str | Path | None = None,
) -> dict | None:
    """Read and validate one exact sidecar without following file symlinks."""
    if not _valid_identity(job_id, operation_id):
        return None
    try:
        path = receipt_path(job_id, operation_id, data_dir=data_dir)
        if path.is_symlink() or not path.is_file():
            return None
        stat = path.stat()
        if stat.st_size <= 0 or stat.st_size > _MAX_RECEIPT_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != 1:
        return None
    if payload.get("job_id") != job_id or payload.get("operation_id") != operation_id:
        return None
    return payload


def remove_publish_receipt(
    job_id: int,
    operation_id: str,
    *,
    data_dir: str | Path | None = None,
) -> None:
    """Best-effort cleanup after the same receipt is durably in the database."""
    if not _valid_identity(job_id, operation_id):
        return
    try:
        receipt_path(job_id, operation_id, data_dir=data_dir).unlink(missing_ok=True)
        job_dir = _receipt_dir(job_id, create=False, data_dir=data_dir)
        try:
            job_dir.rmdir()
        except OSError:
            pass
    except OSError:
        logger.warning("could not remove persisted publish receipt", extra={"job_id": job_id})
