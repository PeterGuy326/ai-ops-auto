"""Safety contract for the local MoneyPrinterTurbo subprocess adapter."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ai_ops.core.schemas import VideoBrief
from ai_ops.video.money_printer import MoneyPrinterEngine


def _configure_local_mpt(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    from ai_ops.video import money_printer as mod

    root = tmp_path / "MoneyPrinterTurbo"
    entry = root / "main.py"
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    entry.write_text("# audited test entry\n", encoding="utf-8")
    (root / ".venv" / "pyvenv.cfg").write_text(
        "home = /usr/bin\n",
        encoding="utf-8",
    )
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o700)
    monkeypatch.setattr(mod.settings, "external_mpt_url", "")
    monkeypatch.setattr(mod.settings, "external_mpt_path", root)
    monkeypatch.setattr(mod.settings, "mpt_python", str(python))
    monkeypatch.setattr(mod.settings, "data_dir", tmp_path / "data")
    return root, python


@pytest.mark.asyncio
async def test_local_health_requires_explicit_executable_inside_mpt_root(
    tmp_path,
    monkeypatch,
):
    from ai_ops.video import money_printer as mod

    _, python = _configure_local_mpt(tmp_path, monkeypatch)
    engine = MoneyPrinterEngine()
    assert await engine.health() is True

    monkeypatch.setattr(mod.settings, "mpt_python", "")
    assert await engine.health() is False

    outside = tmp_path / "outside-python"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    outside.chmod(0o700)
    monkeypatch.setattr(mod.settings, "mpt_python", str(outside))
    assert await engine.health() is False

    monkeypatch.setattr(mod.settings, "mpt_python", str(python))
    python.chmod(0o600)
    assert await engine.health() is False


@pytest.mark.asyncio
async def test_local_render_never_falls_back_to_path_python(tmp_path, monkeypatch):
    from ai_ops.video import money_printer as mod

    _configure_local_mpt(tmp_path, monkeypatch)
    monkeypatch.setattr(mod.settings, "mpt_python", "")

    async def must_not_spawn(*args, **kwargs):
        raise AssertionError("subprocess must not start without explicit MPT_PYTHON")

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", must_not_spawn)
    with pytest.raises(RuntimeError, match="MPT_PYTHON 未配置"):
        await MoneyPrinterEngine().render(VideoBrief(theme="test"))


@pytest.mark.asyncio
async def test_local_render_uses_validated_python_and_minimal_environment(
    tmp_path,
    monkeypatch,
):
    from ai_ops.video import money_printer as mod

    root, python = _configure_local_mpt(tmp_path, monkeypatch)
    monkeypatch.setattr(mod.settings, "browser_engine", "patchright")
    monkeypatch.setenv("API_KEY", "control-secret")
    monkeypatch.setenv("FERNET_KEY", "fernet-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "llm-secret")
    monkeypatch.setenv("PYTHONPATH", "/tmp/host-sitecustomize")
    captured: dict[str, object] = {}

    class FakeProcess:
        returncode = 0
        pid = 12345
        stdout = None
        stderr = None

        async def communicate(self, input_data=None):
            return b"ok", b""

    async def fake_spawn(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        output_dir = Path(argv[argv.index("--output") + 1])
        (output_dir / "generated.mp4").write_bytes(b"video")
        return FakeProcess()

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_spawn)
    artifact = await MoneyPrinterEngine().render(VideoBrief(theme="safe test"))

    argv = captured["argv"]
    kwargs = captured["kwargs"]
    assert argv[0] == str(python)
    assert argv[0] != "python"
    assert argv[1] == str(root / "main.py")
    assert kwargs["cwd"] == str(root)
    assert kwargs["env"]["VIRTUAL_ENV"] == str(root / ".venv")
    assert kwargs["env"]["PATH"].split(mod.os.pathsep, 1)[0] == str(python.parent)
    assert artifact.video_path.endswith("generated.mp4")
    assert Path(artifact.video_path).parent == Path(artifact.meta["run_dir"])
    assert Path(artifact.meta["run_dir"]).parent == tmp_path / "data" / "outputs" / "mpt-cli"
    for key in ("API_KEY", "FERNET_KEY", "OPENAI_API_KEY", "PYTHONPATH", "AI_OPS_STEALTH"):
        assert key not in kwargs["env"]


@pytest.mark.asyncio
async def test_local_render_does_not_reuse_stale_shared_output(tmp_path, monkeypatch):
    from ai_ops.video import money_printer as mod

    _configure_local_mpt(tmp_path, monkeypatch)
    output_root = tmp_path / "data" / "outputs" / "mpt-cli"
    output_root.mkdir(parents=True)
    stale = output_root / "stale.mp4"
    stale.write_bytes(b"old-task")
    captured_output: Path | None = None

    class FakeProcess:
        returncode = 0
        pid = 12345
        stdout = None
        stderr = None

        async def communicate(self, input_data=None):
            return b"ok", b""

    async def fake_spawn(*argv, **kwargs):
        nonlocal captured_output
        captured_output = Path(argv[argv.index("--output") + 1])
        return FakeProcess()

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_spawn)

    with pytest.raises(RuntimeError, match="本轮未产出非空 mp4"):
        await MoneyPrinterEngine().render(VideoBrief(theme="no fresh output"))

    assert captured_output is not None
    assert captured_output != output_root
    assert captured_output.parent == output_root
    assert stale.read_bytes() == b"old-task"


@pytest.mark.asyncio
async def test_local_render_concurrent_runs_have_private_output_dirs(tmp_path, monkeypatch):
    from ai_ops.video import money_printer as mod

    _configure_local_mpt(tmp_path, monkeypatch)
    output_dirs: list[Path] = []

    class FakeProcess:
        returncode = 0
        pid = 12345
        stdout = None
        stderr = None

        async def communicate(self, input_data=None):
            await asyncio.sleep(0)
            return b"ok", b""

    async def fake_spawn(*argv, **kwargs):
        output_dir = Path(argv[argv.index("--output") + 1])
        output_dirs.append(output_dir)
        (output_dir / "generated.mp4").write_bytes(str(output_dir).encode())
        await asyncio.sleep(0)
        return FakeProcess()

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_spawn)

    first, second = await asyncio.gather(
        MoneyPrinterEngine().render(VideoBrief(theme="first")),
        MoneyPrinterEngine().render(VideoBrief(theme="second")),
    )

    assert len(output_dirs) == 2
    assert output_dirs[0] != output_dirs[1]
    assert Path(first.video_path).parent != Path(second.video_path).parent
    assert Path(first.video_path).read_bytes() == str(Path(first.video_path).parent).encode()
    assert Path(second.video_path).read_bytes() == str(Path(second.video_path).parent).encode()


@pytest.mark.asyncio
async def test_local_render_passes_absolute_run_dir_when_data_dir_is_relative(
    tmp_path,
    monkeypatch,
):
    from ai_ops.video import money_printer as mod

    _configure_local_mpt(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mod.settings, "data_dir", Path("relative-data"))
    captured_output: Path | None = None

    class FakeProcess:
        returncode = 0
        pid = 12345
        stdout = None
        stderr = None

        async def communicate(self, input_data=None):
            return b"ok", b""

    async def fake_spawn(*argv, **kwargs):
        nonlocal captured_output
        captured_output = Path(argv[argv.index("--output") + 1])
        (captured_output / "generated.mp4").write_bytes(b"video")
        return FakeProcess()

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_spawn)

    artifact = await MoneyPrinterEngine().render(VideoBrief(theme="relative data"))

    assert captured_output is not None and captured_output.is_absolute()
    assert captured_output.parent == tmp_path / "relative-data" / "outputs" / "mpt-cli"
    assert Path(artifact.video_path).is_absolute()


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact_kind", ["empty", "symlink"])
async def test_local_render_rejects_non_artifact_mp4(
    tmp_path,
    monkeypatch,
    artifact_kind,
):
    from ai_ops.video import money_printer as mod

    _configure_local_mpt(tmp_path, monkeypatch)
    old_video = tmp_path / "old.mp4"
    old_video.write_bytes(b"old-task")

    class FakeProcess:
        returncode = 0
        pid = 12345
        stdout = None
        stderr = None

        async def communicate(self, input_data=None):
            return b"ok", b""

    async def fake_spawn(*argv, **kwargs):
        output_dir = Path(argv[argv.index("--output") + 1])
        candidate = output_dir / "generated.mp4"
        if artifact_kind == "empty":
            candidate.write_bytes(b"")
        else:
            candidate.symlink_to(old_video)
        return FakeProcess()

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", fake_spawn)

    with pytest.raises(RuntimeError, match="本轮未产出非空 mp4"):
        await MoneyPrinterEngine().render(VideoBrief(theme=artifact_kind))
