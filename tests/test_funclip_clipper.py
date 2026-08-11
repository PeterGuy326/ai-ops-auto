"""FunClipClipper 单测 —— 全 mock subprocess，不依赖真实 FunClip 环境。

测试目标：
  1. parse_srt 纯函数：标准 SRT、毫秒分隔符 . vs ,、空块、缺序号
  2. _build_stage1_argv / _build_stage2_argv 命令拼接
  3. health() 静态校验：路径不存在返 False
  4. transcribe()：subprocess 返 0 + 写 SRT → 返 TranscriptResult
  5. transcribe()：subprocess 非零 → RuntimeError 带 stderr
  6. transcribe()：无 SRT 产物 → RuntimeError
  7. clip()：segments 空 → ValueError
  8. clip()：成功路径 → ClipResult.clips 数量对、路径对
  9. _run timeout：subprocess wait_for 超时 → TimeoutError
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_ops.core.enums import VideoClipperKind
from ai_ops.core.schemas import ClipRequest, ClipSegment
from ai_ops.video.clipper.funclip import FunClipClipper, parse_srt, _srt_ts_to_ms


# ---------------- parse_srt ----------------

SRT_SAMPLE = """1
00:00:00,000 --> 00:00:02,500
我们把它跟乡村振兴去结合起来

2
00:00:02,500 --> 00:00:05,800
利用我们的设计的能力

3
00:00:05,800 --> 00:00:08,000
这个技术的核心是什么呢
"""


@pytest.fixture(autouse=True)
def _explicit_funclip_python(tmp_path, monkeypatch):
    """Keep argv-only tests explicit without creating a real external runtime."""
    from ai_ops.video.clipper import funclip as mod

    monkeypatch.setattr(
        mod.settings,
        "funclip_python",
        str(tmp_path / "configured-funclip" / ".venv" / "bin" / "python"),
    )


def test_srt_ts_to_ms_comma_and_dot():
    assert _srt_ts_to_ms("00:00:01,500") == 1500
    assert _srt_ts_to_ms("00:00:01.500") == 1500
    assert _srt_ts_to_ms("01:02:03,456") == (3600 + 120 + 3) * 1000 + 456


def test_parse_srt_three_cues():
    cues = parse_srt(SRT_SAMPLE)
    assert len(cues) == 3
    assert cues[0].index == 1
    assert cues[0].start_ms == 0
    assert cues[0].end_ms == 2500
    assert "乡村振兴" in cues[0].text
    assert cues[2].start_ms == 5800


def test_parse_srt_empty_returns_empty():
    assert parse_srt("") == []
    assert parse_srt("\n\n\n") == []


def test_parse_srt_skips_malformed_block():
    bad = "not a valid block\n\n2\n00:00:01,000 --> 00:00:02,000\nok\n"
    cues = parse_srt(bad)
    assert len(cues) == 1
    assert cues[0].text == "ok"


# ---------------- argv 拼接 ----------------

def test_kind_is_funclip():
    assert FunClipClipper.kind == VideoClipperKind.FUNCLIP


def test_build_stage1_argv():
    c = FunClipClipper()
    argv = c._build_stage1_argv("/tmp/a.mp4", Path("/tmp/out"))
    assert "--stage" in argv and argv[argv.index("--stage") + 1] == "1"
    assert "--file" in argv and argv[argv.index("--file") + 1] == "/tmp/a.mp4"
    assert "--output_dir" in argv and argv[argv.index("--output_dir") + 1] == "/tmp/out"


def test_build_stage2_argv_with_dest_text():
    c = FunClipClipper()
    seg = ClipSegment(dest_text="乡村振兴", start_ost_ms=100, end_ost_ms=200)
    argv = c._build_stage2_argv("/tmp/a.mp4", Path("/tmp/out"), seg, Path("/tmp/out/clip_001.mp4"))
    assert argv[argv.index("--stage") + 1] == "2"
    assert argv[argv.index("--dest_text") + 1] == "乡村振兴"
    assert argv[argv.index("--start_ost") + 1] == "100"
    assert argv[argv.index("--end_ost") + 1] == "200"
    assert argv[argv.index("--output_file") + 1] == "/tmp/out/clip_001.mp4"


def test_build_stage2_argv_without_dest_text_omits_flag():
    c = FunClipClipper()
    seg = ClipSegment(start_ms=1000, end_ms=3000)
    argv = c._build_stage2_argv("/tmp/a.mp4", Path("/tmp/out"), seg, Path("/tmp/out/clip_001.mp4"))
    assert "--dest_text" not in argv


def test_python_entry_root_are_absolute(monkeypatch):
    """回归：子进程 cwd 会切到 FunClip 仓库根，所以 _python / _entry /
    _funclip_root 必须返绝对路径——否则相对路径会被叠成
    external/FunClip/external/FunClip/... 触发 FileNotFoundError。
    """
    from ai_ops.video.clipper import funclip as mod

    monkeypatch.setattr(mod.settings, "funclip_path", Path("./external/FunClip"))
    monkeypatch.setattr(
        mod.settings, "funclip_python", "./external/FunClip/.venv/bin/python"
    )
    c = FunClipClipper()
    assert c._funclip_root().is_absolute()
    assert c._entry().is_absolute()
    assert Path(c._python()).is_absolute()


def test_python_fails_closed_when_unconfigured(monkeypatch):
    from ai_ops.video.clipper import funclip as mod

    monkeypatch.setattr(mod.settings, "funclip_python", "")
    with pytest.raises(RuntimeError, match="FUNCLIP_PYTHON 未配置"):
        FunClipClipper()._python()


def test_python_does_not_follow_symlink(tmp_path, monkeypatch):
    """回归：venv 的 bin/python 是指向系统 python 的 symlink。若 _python()
    用 resolve() 跟随该 symlink，子进程就用系统 python 启动、脱离 venv
    site-packages，FunClip 装在 venv 里的 librosa 等会 ModuleNotFoundError。
    _python() 必须保留 symlink 路径本身。
    """
    from ai_ops.video.clipper import funclip as mod

    real = tmp_path / "real_python"
    real.write_text("#!/bin/sh\n")
    link = tmp_path / "venv_python"
    link.symlink_to(real)
    monkeypatch.setattr(mod.settings, "funclip_python", str(link))
    c = FunClipClipper()
    assert c._python() == str(link)
    assert c._python() != str(real)


def test_subprocess_env_preserves_model_gpu_controls_without_browser_injection(
    monkeypatch,
):
    from ai_ops.video.clipper import funclip as mod

    monkeypatch.setattr(mod.settings, "browser_engine", "patchright")
    monkeypatch.setattr(mod.settings, "funclip_python", "")
    source = {
        "PATH": "/usr/bin",
        "HOME": "/tmp/model-home",
        "VIRTUAL_ENV": "/control-plane/.venv",
        "CUDA_VISIBLE_DEVICES": "2",
        "NVIDIA_VISIBLE_DEVICES": "2",
        "PYTORCH_CUDA_ALLOC_CONF": "max_split_size_mb:128",
        "TORCH_HOME": "/models/torch",
        "HF_HOME": "/models/hf",
        "MODELSCOPE_CACHE": "/models/modelscope",
        "LD_LIBRARY_PATH": "/opt/cuda/lib64",
        "API_KEY": "control-secret",
        "FERNET_KEY": "fernet-secret",
        "OPENAI_API_KEY": "llm-secret",
        "HF_TOKEN": "model-secret",
        "PYTHONPATH": "/tmp/host-sitecustomize",
        "AI_OPS_STEALTH": "patchright",
    }

    env = FunClipClipper()._subprocess_env(source)

    for key in (
        "CUDA_VISIBLE_DEVICES",
        "NVIDIA_VISIBLE_DEVICES",
        "PYTORCH_CUDA_ALLOC_CONF",
        "TORCH_HOME",
        "HF_HOME",
        "MODELSCOPE_CACHE",
        "LD_LIBRARY_PATH",
    ):
        assert env[key] == source[key]
    for key in (
        "API_KEY",
        "FERNET_KEY",
        "OPENAI_API_KEY",
        "HF_TOKEN",
        "PYTHONPATH",
        "AI_OPS_STEALTH",
        "VIRTUAL_ENV",
    ):
        assert key not in env


@pytest.mark.asyncio
async def test_run_passes_minimal_funclip_venv_environment(tmp_path, monkeypatch):
    """_run 必须显式传最小环境，不能因 env=None 继承控制面 secrets。"""
    from ai_ops.video.clipper import funclip as mod

    root = tmp_path / "FunClip"
    python = root / ".venv" / "bin" / "python"
    entry = root / "funclip" / "videoclipper.py"
    venv_root = python.parent.parent
    clipper = FunClipClipper()
    monkeypatch.setattr(
        clipper,
        "_runtime_boundary",
        lambda: (root, python, entry, venv_root),
    )
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "3")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    captured = {}
    process = SimpleNamespace(returncode=0)

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return process

    async def fake_communicate_bounded(actual_process):
        assert actual_process is process
        return b"ok", b""

    monkeypatch.setattr(
        mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    monkeypatch.setattr(mod, "communicate_bounded", fake_communicate_bounded)

    result = await clipper._run([str(python), str(entry)], cwd=root)

    assert result == (0, "ok", "")
    child_env = captured["kwargs"]["env"]
    assert child_env is not None
    assert child_env["CUDA_VISIBLE_DEVICES"] == "3"
    assert child_env["VIRTUAL_ENV"] == str(venv_root)
    assert child_env["PATH"].split(":", 1)[0] == str(python.parent)
    assert "OPENAI_API_KEY" not in child_env
    assert captured["kwargs"]["stdin"] is mod.asyncio.subprocess.DEVNULL


# ---------------- health ----------------

@pytest.mark.asyncio
async def test_health_false_when_path_missing(tmp_path, monkeypatch):
    from ai_ops.video.clipper import funclip as mod

    monkeypatch.setattr(mod.settings, "funclip_path", tmp_path / "nope")
    c = FunClipClipper()
    assert await c.health() is False


@pytest.mark.asyncio
async def test_health_false_when_entry_missing(tmp_path, monkeypatch):
    from ai_ops.video.clipper import funclip as mod

    monkeypatch.setattr(mod.settings, "funclip_path", tmp_path)
    c = FunClipClipper()
    assert await c.health() is False


@pytest.mark.asyncio
async def test_health_true_when_path_and_entry_exist(tmp_path, monkeypatch):
    from ai_ops.video.clipper import funclip as mod

    entry = tmp_path / "funclip" / "videoclipper.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("# fake")
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    python.chmod(0o700)
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    monkeypatch.setattr(mod.settings, "funclip_path", tmp_path)
    monkeypatch.setattr(mod.settings, "funclip_python", str(python))
    c = FunClipClipper()
    assert await c.health() is True


@pytest.mark.asyncio
async def test_health_false_when_python_is_outside_funclip_root(tmp_path, monkeypatch):
    from ai_ops.video.clipper import funclip as mod

    root = tmp_path / "FunClip"
    entry = root / "funclip" / "videoclipper.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("# fake")
    outside_python = tmp_path / "outside" / ".venv" / "bin" / "python"
    outside_python.parent.mkdir(parents=True)
    outside_python.write_text("#!/bin/sh\n")
    outside_python.chmod(0o700)
    (outside_python.parent.parent / "pyvenv.cfg").write_text("home = /usr/bin\n")
    monkeypatch.setattr(mod.settings, "funclip_path", root)
    monkeypatch.setattr(mod.settings, "funclip_python", str(outside_python))

    assert await FunClipClipper().health() is False


@pytest.mark.asyncio
async def test_health_false_when_custom_python_missing(tmp_path, monkeypatch):
    from ai_ops.video.clipper import funclip as mod

    entry = tmp_path / "funclip" / "videoclipper.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("# fake")
    monkeypatch.setattr(mod.settings, "funclip_path", tmp_path)
    monkeypatch.setattr(mod.settings, "funclip_python", str(tmp_path / "no-such-python"))
    c = FunClipClipper()
    assert await c.health() is False


# ---------------- transcribe / clip ----------------

def _fake_run_factory(returncode=0, stdout="ok", stderr=""):
    """造一个 mock 版的 _run，匹配 (int, str, str) 返回签名。"""
    async def _run(argv, cwd=None):
        return returncode, stdout, stderr
    return _run


@pytest.mark.asyncio
async def test_transcribe_writes_srt_returns_cues(tmp_path, monkeypatch):
    from ai_ops.video.clipper import funclip as mod

    monkeypatch.setattr(mod.settings, "funclip_path", tmp_path)
    # mock _run：跑成功，并顺手把 SRT 写到 output_dir，让真实文件扫描能找到
    out_dir_holder = {}

    async def fake_run(argv, cwd=None):
        # 从 argv 里抠出 output_dir，写 SRT
        idx = argv.index("--output_dir")
        out_dir = Path(argv[idx + 1])
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "res.srt").write_text(SRT_SAMPLE, encoding="utf-8")
        out_dir_holder["path"] = out_dir
        return 0, "ok", ""

    c = FunClipClipper()
    monkeypatch.setattr(c, "_run", fake_run)

    result = await c.transcribe("/tmp/in.mp4", str(tmp_path / "out"))
    assert result.srt_path.endswith("res.srt")
    assert Path(result.srt_path).parent == Path(result.meta["run_dir"])
    assert Path(result.meta["run_dir"]).parent == tmp_path / "out"
    assert result.meta["run_id"]
    assert len(result.cues) == 3
    assert "乡村振兴" in result.full_text


@pytest.mark.asyncio
async def test_transcribe_raises_on_nonzero_exit(tmp_path, monkeypatch):
    c = FunClipClipper()
    monkeypatch.setattr(c, "_run", _fake_run_factory(returncode=2, stderr="boom"))
    with pytest.raises(RuntimeError, match="stage 1 failed"):
        await c.transcribe("/tmp/in.mp4", str(tmp_path / "out"))


@pytest.mark.asyncio
async def test_transcribe_raises_when_no_srt(tmp_path, monkeypatch):
    c = FunClipClipper()
    # _run 成功但不写 SRT
    monkeypatch.setattr(c, "_run", _fake_run_factory(returncode=0))
    with pytest.raises(RuntimeError, match="did not produce any .srt"):
        await c.transcribe("/tmp/in.mp4", str(tmp_path / "out"))


@pytest.mark.asyncio
async def test_transcribe_rejects_empty_and_symlink_srt(tmp_path, monkeypatch):
    """stage1 回执必须是本轮目录里的非空普通文件。"""
    external_srt = tmp_path / "old.srt"
    external_srt.write_text(SRT_SAMPLE, encoding="utf-8")

    async def fake_run(argv, cwd=None):
        out = Path(argv[argv.index("--output_dir") + 1])
        (out / "empty.srt").touch()
        (out / "linked.srt").symlink_to(external_srt)
        return 0, "ok", ""

    clipper = FunClipClipper()
    monkeypatch.setattr(clipper, "_run", fake_run)

    with pytest.raises(RuntimeError, match="non-empty regular file"):
        await clipper.transcribe("/tmp/in.mp4", str(tmp_path / "out"))


@pytest.mark.asyncio
async def test_clip_empty_segments_raises():
    c = FunClipClipper()
    with pytest.raises(ValueError, match="at least one segment"):
        await c.clip(ClipRequest(input_video="/tmp/in.mp4", segments=[]))


@pytest.mark.asyncio
async def test_clip_happy_path_two_segments(tmp_path, monkeypatch):
    from ai_ops.video.clipper import funclip as mod

    monkeypatch.setattr(mod.settings, "funclip_path", tmp_path)

    call_log: list[list[str]] = []

    async def fake_run(argv, cwd=None):
        call_log.append(argv)
        if argv[argv.index("--stage") + 1] == "1":
            out_dir = Path(argv[argv.index("--output_dir") + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "res.srt").write_text(SRT_SAMPLE, encoding="utf-8")
        else:  # stage 2：FunClip 实际产物是 <stem>_no0.mp4，不是 --output_file 原名
            of = Path(argv[argv.index("--output_file") + 1])
            of.parent.mkdir(parents=True, exist_ok=True)
            (of.parent / f"{of.stem}_no0.mp4").write_bytes(b"fake mp4")
        return 0, "ok", ""

    c = FunClipClipper()
    monkeypatch.setattr(c, "_run", fake_run)

    req = ClipRequest(
        input_video="/tmp/in.mp4",
        segments=[
            ClipSegment(dest_text="乡村振兴", start_ost_ms=0, end_ost_ms=0),
            ClipSegment(dest_text="设计的能力", start_ost_ms=100, end_ost_ms=200),
        ],
        output_dir=str(tmp_path / "clips"),
    )
    result = await c.clip(req)
    assert len(result.clips) == 2
    assert result.clips[0].dest_text == "乡村振兴"
    assert result.clips[0].video_path.endswith("_no0.mp4")
    assert Path(result.clips[0].video_path).exists()
    assert result.clips[1].meta["start_ost_ms"] == 100
    # 至少触发 stage1 + 2 次 stage2 = 3 次 _run
    assert len(call_log) == 3
    assert result.transcript is not None and len(result.transcript.cues) == 3


@pytest.mark.asyncio
async def test_clip_retries_uuid_collision_without_reusing_stale_artifacts(
    tmp_path, monkeypatch
):
    """已有同名目录时必须换 run id，不能复用里面的旧 SRT/clip。"""
    from ai_ops.video.clipper import funclip as mod

    output_root = tmp_path / "clips"
    collision_id = "0" * 32
    fresh_id = "1" * 32
    stale_dir = output_root / f"funclip_{collision_id}"
    stale_dir.mkdir(parents=True)
    (stale_dir / "res.srt").write_text("旧字幕", encoding="utf-8")
    (stale_dir / "clip_001_no0.mp4").write_bytes(b"stale clip")

    ids = iter(
        [SimpleNamespace(hex=collision_id), SimpleNamespace(hex=fresh_id)]
    )
    monkeypatch.setattr(mod, "uuid4", lambda: next(ids))
    monkeypatch.setattr(mod.settings, "funclip_path", tmp_path)

    async def fake_run(argv, cwd=None):
        out_dir = Path(argv[argv.index("--output_dir") + 1])
        if argv[argv.index("--stage") + 1] == "1":
            (out_dir / "res.srt").write_text(SRT_SAMPLE, encoding="utf-8")
        else:
            output_file = Path(argv[argv.index("--output_file") + 1])
            (out_dir / f"{output_file.stem}_no0.mp4").write_bytes(b"fresh clip")
        return 0, "ok", ""

    clipper = FunClipClipper()
    monkeypatch.setattr(clipper, "_run", fake_run)
    result = await clipper.clip(
        ClipRequest(
            input_video="/tmp/in.mp4",
            segments=[ClipSegment(dest_text="乡村振兴")],
            output_dir=str(output_root),
        )
    )

    run_dir = Path(result.meta["run_dir"])
    assert run_dir == output_root / f"funclip_{fresh_id}"
    assert result.meta["run_id"] == fresh_id
    assert Path(result.transcript.srt_path).parent == run_dir
    assert Path(result.clips[0].video_path).parent == run_dir
    assert Path(result.clips[0].video_path).read_bytes() == b"fresh clip"
    assert (stale_dir / "clip_001_no0.mp4").read_bytes() == b"stale clip"


@pytest.mark.asyncio
async def test_concurrent_clip_calls_use_distinct_runs_and_exact_artifacts(
    tmp_path, monkeypatch
):
    """同一 output root 的并发调用必须完全隔离，并忽略近似文件名。"""
    from ai_ops.video.clipper import funclip as mod

    run_ids = ("a" * 32, "b" * 32)
    ids = iter(SimpleNamespace(hex=value) for value in run_ids)
    monkeypatch.setattr(mod, "uuid4", lambda: next(ids))
    monkeypatch.setattr(mod.settings, "funclip_path", tmp_path)

    async def fake_run(argv, cwd=None):
        await asyncio.sleep(0)
        out_dir = Path(argv[argv.index("--output_dir") + 1])
        if argv[argv.index("--stage") + 1] == "1":
            (out_dir / "res.srt").write_text(SRT_SAMPLE, encoding="utf-8")
        else:
            output_file = Path(argv[argv.index("--output_file") + 1])
            (out_dir / f"{output_file.stem}_no0.mp4").write_bytes(b"exact")
            (out_dir / f"{output_file.stem}_no0_old.mp4").write_bytes(b"impostor")
        return 0, "ok", ""

    clipper = FunClipClipper()
    monkeypatch.setattr(clipper, "_run", fake_run)
    request = ClipRequest(
        input_video="/tmp/in.mp4",
        segments=[ClipSegment(dest_text="乡村振兴")],
        output_dir=str(tmp_path / "clips"),
    )

    results = await asyncio.gather(clipper.clip(request), clipper.clip(request))

    actual_run_dirs = {Path(result.meta["run_dir"]) for result in results}
    assert actual_run_dirs == {
        tmp_path / "clips" / f"funclip_{run_id}" for run_id in run_ids
    }
    for result in results:
        assert len(result.clips) == 1
        run_dir = Path(result.meta["run_dir"])
        assert Path(result.transcript.srt_path).parent == run_dir
        assert Path(result.clips[0].video_path).parent == run_dir
        assert Path(result.clips[0].video_path).name == "clip_001_no0.mp4"


@pytest.mark.asyncio
async def test_clip_raises_when_stage2_produces_no_file(tmp_path, monkeypatch):
    """回归：FunClip dest_text 无命中时走 else 分支——不写切片文件，但退出码
    仍是 0。wrapper 必须扫描真实产物、察觉空结果并报错，而不是返回一个
    根本不存在的预期路径。
    """
    from ai_ops.video.clipper import funclip as mod

    monkeypatch.setattr(mod.settings, "funclip_path", tmp_path)

    async def fake_run(argv, cwd=None):
        if argv[argv.index("--stage") + 1] == "1":
            out = Path(argv[argv.index("--output_dir") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "res.srt").write_text(SRT_SAMPLE, encoding="utf-8")
        # stage 2：退出码 0 但不写任何 _no*.mp4
        return 0, "No period found in the audio", ""

    c = FunClipClipper()
    monkeypatch.setattr(c, "_run", fake_run)

    req = ClipRequest(
        input_video="/tmp/in.mp4",
        segments=[ClipSegment(dest_text="转写里不存在的句子")],
        output_dir=str(tmp_path / "clips"),
    )
    with pytest.raises(RuntimeError, match="produced no clip"):
        await c.clip(req)


@pytest.mark.asyncio
async def test_clip_rejects_empty_and_symlink_stage2_artifacts(tmp_path, monkeypatch):
    """退出码 0 也不能把空文件或指向目录外的 symlink 当成切片回执。"""
    external_clip = tmp_path / "old.mp4"
    external_clip.write_bytes(b"old clip")

    async def fake_run(argv, cwd=None):
        out = Path(argv[argv.index("--output_dir") + 1])
        if argv[argv.index("--stage") + 1] == "1":
            (out / "res.srt").write_text(SRT_SAMPLE, encoding="utf-8")
        else:
            output_file = Path(argv[argv.index("--output_file") + 1])
            (out / f"{output_file.stem}_no0.mp4").symlink_to(external_clip)
            (out / f"{output_file.stem}_no1.mp4").touch()
        return 0, "ok", ""

    clipper = FunClipClipper()
    monkeypatch.setattr(clipper, "_run", fake_run)
    request = ClipRequest(
        input_video="/tmp/in.mp4",
        segments=[ClipSegment(dest_text="乡村振兴")],
        output_dir=str(tmp_path / "clips"),
    )

    with pytest.raises(RuntimeError, match="produced no clip"):
        await clipper.clip(request)


@pytest.mark.asyncio
async def test_clip_stage2_failure_raises_with_index(tmp_path, monkeypatch):
    from ai_ops.video.clipper import funclip as mod

    monkeypatch.setattr(mod.settings, "funclip_path", tmp_path)

    async def fake_run(argv, cwd=None):
        if argv[argv.index("--stage") + 1] == "1":
            out = Path(argv[argv.index("--output_dir") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "res.srt").write_text(SRT_SAMPLE, encoding="utf-8")
            return 0, "ok", ""
        # stage 2 总是炸
        return 3, "", "stage2-error"

    c = FunClipClipper()
    monkeypatch.setattr(c, "_run", fake_run)

    req = ClipRequest(
        input_video="/tmp/in.mp4",
        segments=[ClipSegment(dest_text="乡村振兴")],
        output_dir=str(tmp_path / "clips"),
    )
    with pytest.raises(RuntimeError, match="stage 2 failed at seg #1"):
        await c.clip(req)
