from __future__ import annotations

import asyncio
import os
import sys

from ai_ops.runtime.subprocess import communicate_bounded


async def test_communicate_bounded_drains_but_caps_retained_output() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(b'x'*300000); "
        "sys.stderr.buffer.write(b'y'*100000)",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )

    stdout, stderr = await asyncio.wait_for(
        communicate_bounded(proc, stdout_limit=4096, stderr_limit=2048),
        timeout=10,
    )

    assert proc.returncode == 0
    assert stdout == b"x" * 4096
    assert stderr == b"y" * 2048


async def test_communicate_bounded_supports_stdin_without_argv_content() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )

    stdout, stderr = await communicate_bounded(proc, input_data=b"private prompt")

    assert proc.returncode == 0
    assert stdout == b"private prompt"
    assert stderr == b""
