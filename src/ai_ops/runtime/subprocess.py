"""Fail-closed subprocess helpers shared by CLI and media adapters."""
from __future__ import annotations

import asyncio
import os
import signal


DEFAULT_STDOUT_LIMIT = 256 * 1024
DEFAULT_STDERR_LIMIT = 64 * 1024


async def _read_bounded(stream, limit: int) -> bytes:
    """Drain an async pipe completely while retaining at most ``limit`` bytes."""
    if stream is None:
        return b""
    retained = bytearray()
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        remaining = max(0, limit - len(retained))
        if remaining:
            retained.extend(chunk[:remaining])
    return bytes(retained)


async def _write_input(proc: asyncio.subprocess.Process, input_data: bytes | None) -> None:
    if input_data is None or proc.stdin is None:
        return
    proc.stdin.write(input_data)
    await proc.stdin.drain()
    proc.stdin.close()
    try:
        await proc.stdin.wait_closed()
    except (AttributeError, BrokenPipeError, ConnectionResetError):
        pass


async def communicate_bounded(
    proc: asyncio.subprocess.Process,
    *,
    input_data: bytes | None = None,
    stdout_limit: int = DEFAULT_STDOUT_LIMIT,
    stderr_limit: int = DEFAULT_STDERR_LIMIT,
) -> tuple[bytes, bytes]:
    """Wait for a child while bounding retained untrusted CLI output."""
    stdout = getattr(proc, "stdout", None)
    stderr = getattr(proc, "stderr", None)
    if not hasattr(stdout, "read") and not hasattr(stderr, "read"):
        if input_data is None:
            out, err = await proc.communicate()
        else:
            out, err = await proc.communicate(input_data)
        return (out or b"")[:stdout_limit], (err or b"")[:stderr_limit]
    out, err, _, _ = await asyncio.gather(
        _read_bounded(stdout, stdout_limit),
        _read_bounded(stderr, stderr_limit),
        _write_input(proc, input_data),
        proc.wait(),
    )
    return out, err


def _process_group_alive(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def stop_process_group(
    proc: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 3.0,
) -> None:
    """Terminate and reap a subprocess plus descendants in its POSIX session."""
    process_group_id = proc.pid if os.name == "posix" and proc.pid else None

    async def drain(timeout: float) -> bool:
        try:
            await asyncio.wait_for(proc.communicate(), timeout=max(0.01, timeout))
            return True
        except (TimeoutError, RuntimeError, ChildProcessError):
            return False

    group_alive = bool(
        process_group_id is not None and _process_group_alive(process_group_id)
    )
    if proc.returncode is None or group_alive:
        try:
            if process_group_id is not None:
                os.killpg(process_group_id, signal.SIGTERM)
            elif proc.returncode is None:
                proc.terminate()
        except (ProcessLookupError, PermissionError):
            pass

    await drain(grace_seconds)

    group_alive = bool(
        process_group_id is not None and _process_group_alive(process_group_id)
    )
    if proc.returncode is None or group_alive:
        try:
            if process_group_id is not None:
                os.killpg(process_group_id, signal.SIGKILL)
            elif proc.returncode is None:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass

    await drain(1.0)
    if proc.returncode is None:
        try:
            await proc.wait()
        except (ChildProcessError, RuntimeError):
            pass
