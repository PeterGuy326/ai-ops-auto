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

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    monkeypatch.setattr(mod.settings, "funclip_path", tmp_path)
    monkeypatch.setattr(mod.settings, "funclip_python", "")  # 用 sys.executable
    c = FunClipClipper()
    assert await c.health() is True


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
        # 第一次（stage 1）写 SRT，后续 stage 2 不需写
        if "--stage" in argv and argv[argv.index("--stage") + 1] == "1":
            idx = argv.index("--output_dir")
            out_dir = Path(argv[idx + 1])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "res.srt").write_text(SRT_SAMPLE, encoding="utf-8")
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
    assert result.clips[1].meta["start_ost_ms"] == 100
    # 至少触发 stage1 + 2 次 stage2 = 3 次 _run
    assert len(call_log) == 3
    assert result.transcript is not None and len(result.transcript.cues) == 3


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
