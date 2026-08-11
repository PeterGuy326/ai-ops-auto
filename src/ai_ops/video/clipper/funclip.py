"""modelscope/FunClip 集成 wrapper（subprocess + CLI）。

底层逻辑：
  - FunClip 依赖体积巨大（torch + funasr + modelscope + GB 级模型权重），
    跟主项目的 playwright/camoufox 共存容易冲突，所以走「外置 + subprocess」隔离档。
  - 上游 CLI 入口：funclip/videoclipper.py，两阶段：
      stage 1 → ASR（产出 SRT + recog_res）
      stage 2 → 按 dest_text / 时间段剪辑
  - 本 wrapper 只做：拼命令、起子进程、解析 SRT、汇总切片路径。
    不 import funclip，不依赖 funasr，不污染主 venv。

配置入口：see ai_ops.config.Settings.funclip_*
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import stat
from pathlib import Path
from typing import Optional
from uuid import uuid4

from ...config import settings
from ...core.enums import VideoClipperKind
from ...core.schemas import (
    ClipArtifact,
    ClipRequest,
    ClipResult,
    ClipSegment,
    TranscriptCue,
    TranscriptResult,
)
from ...runtime.browser_engine import build_subprocess_env
from ...runtime.subprocess import communicate_bounded, stop_process_group
from ..clipper_base import VideoClipperBase


# SRT 时间戳：HH:MM:SS,mmm 或 HH:MM:SS.mmm（FunClip 实测两种都出现过）
_SRT_TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")

# FunClip/FunASR runs in an isolated venv but still needs selected accelerator
# and model-cache controls from its host/container.  This list intentionally
# excludes cloud credentials and control-plane secrets.
_FUNCLIP_ENV_ALLOWLIST = {
    "CUDA_VISIBLE_DEVICES",
    "CUDA_DEVICE_ORDER",
    "CUDA_HOME",
    "CUDA_CACHE_PATH",
    "NVIDIA_VISIBLE_DEVICES",
    "NVIDIA_DRIVER_CAPABILITIES",
    "ROCR_VISIBLE_DEVICES",
    "HIP_VISIBLE_DEVICES",
    "HSA_OVERRIDE_GFX_VERSION",
    "PYTORCH_CUDA_ALLOC_CONF",
    "PYTORCH_ENABLE_MPS_FALLBACK",
    "TORCH_HOME",
    "HF_HOME",
    "HUGGINGFACE_HUB_CACHE",
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_CACHE",
    "TRANSFORMERS_OFFLINE",
    "MODELSCOPE_CACHE",
    "MODELSCOPE_DOMAIN",
    "FUNASR_HOME",
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "TOKENIZERS_PARALLELISM",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
}


def _srt_ts_to_ms(ts: str) -> int:
    m = _SRT_TS.search(ts)
    if not m:
        raise ValueError(f"invalid SRT timestamp: {ts!r}")
    h, mi, s, ms = m.groups()
    return ((int(h) * 60 + int(mi)) * 60 + int(s)) * 1000 + int(ms[:3].ljust(3, "0"))


def parse_srt(srt_text: str) -> list[TranscriptCue]:
    """简易 SRT 解析——只取 index/时间/文本，不依赖第三方 pysrt。"""
    cues: list[TranscriptCue] = []
    blocks = re.split(r"\n\s*\n", srt_text.strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        # 第一行可能是 index，也可能直接是时间轴（FunClip 多数情况第一行是序号）
        idx_line, ts_line, *text_lines = lines if "-->" in lines[1] else ["0", *lines]
        if "-->" not in ts_line:
            continue
        try:
            start_str, end_str = [s.strip() for s in ts_line.split("-->")]
            idx = int(idx_line) if idx_line.strip().isdigit() else len(cues) + 1
            cues.append(
                TranscriptCue(
                    index=idx,
                    start_ms=_srt_ts_to_ms(start_str),
                    end_ms=_srt_ts_to_ms(end_str),
                    text=" ".join(text_lines).strip(),
                )
            )
        except (ValueError, IndexError):
            continue
    return cues


class FunClipClipper(VideoClipperBase):
    kind = VideoClipperKind.FUNCLIP

    # ---------- 内部工具 ----------

    def _funclip_root(self) -> Path:
        """FunClip 仓库根的绝对路径。

        子进程 cwd 必须设在这里——FunClip 内部用相对路径找 font/ 等资源。
        正因为 cwd 会变，所有传给子进程的路径（python / entry / file /
        output_dir / output_file）都必须先转成绝对路径，否则相对路径
        会被叠加成 external/FunClip/external/FunClip/... 而找不到。

        用 os.path.abspath 而非 Path.resolve()：resolve() 会跟随符号链接，
        见 _python() 的说明。
        """
        return Path(os.path.abspath(settings.funclip_path))

    def _python(self) -> str:
        """FunClip venv 的 python 解释器，绝对路径。

        关键：必须用 os.path.abspath，不能用 Path.resolve()。venv 的
        bin/python 是指向系统 python 的符号链接，resolve() 会跟随它解析成
        /usr/bin/pythonX.Y——一旦用系统 python 启动就脱离 venv 的
        site-packages，FunClip 装在 venv 里的 librosa/funasr 等会全部
        ModuleNotFoundError。abspath 只转绝对、不跟随 symlink，保住 venv 上下文。
        """
        configured = settings.funclip_python.strip()
        if not configured:
            raise RuntimeError(
                "FUNCLIP_PYTHON 未配置；必须使用 FUNCLIP_PATH 内的独立 venv 解释器"
            )
        return os.path.abspath(os.path.expanduser(configured))

    def _entry(self) -> Path:
        return self._funclip_root() / "funclip" / "videoclipper.py"

    def _ensure_dir(self, p: Path) -> Path:
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _create_run_dir(self, output_root: str | Path) -> tuple[Path, str]:
        """Atomically create a fresh directory for one clip invocation.

        ``mkdir(exist_ok=False)`` is deliberate: even an astronomically unlikely
        UUID collision, or a deterministic collision with a stale directory, must
        never make a new invocation consume old SRT/MP4 artifacts.
        """
        root = self._ensure_dir(Path(os.path.abspath(output_root)))
        for _ in range(16):
            run_id = uuid4().hex
            run_dir = root / f"funclip_{run_id}"
            try:
                run_dir.mkdir(exist_ok=False)
            except FileExistsError:
                continue
            return run_dir, run_id
        raise RuntimeError("FunClip 无法创建唯一运行目录")

    @staticmethod
    def _is_current_artifact(candidate: Path, run_dir: Path) -> bool:
        """Accept only a non-empty regular file physically contained in run_dir."""
        try:
            if candidate.is_symlink():
                return False
            file_stat = candidate.stat(follow_symlinks=False)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size <= 0:
                return False
            candidate.resolve(strict=True).relative_to(run_dir.resolve(strict=True))
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            return False
        return True

    def _runtime_boundary(self) -> tuple[Path, Path, Path, Path]:
        """Validate the pinned FunClip repo entry and isolated venv."""
        root = self._funclip_root()
        if not root.is_dir():
            raise RuntimeError(f"FUNCLIP_PATH 不存在或不是目录: {root}")
        entry = self._entry()
        if not entry.is_file() or entry.is_symlink():
            raise RuntimeError(f"FunClip CLI 入口无效: {entry}")

        python = Path(self._python())
        try:
            python.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(
                f"FUNCLIP_PYTHON 必须位于 FUNCLIP_PATH 内: {python}"
            ) from exc
        if not python.is_file() or not os.access(python, os.X_OK):
            raise RuntimeError(f"FUNCLIP_PYTHON 不存在或不可执行: {python}")
        venv_root = python.parent.parent
        if not (venv_root / "pyvenv.cfg").is_file():
            raise RuntimeError(
                "FUNCLIP_PYTHON 必须来自独立 venv "
                f"（缺少 {venv_root / 'pyvenv.cfg'}）"
            )
        return root, python, entry, venv_root

    def _subprocess_env(
        self,
        base_env: dict | None = None,
        *,
        python: Path | None = None,
        venv_root: Path | None = None,
    ) -> dict[str, str]:
        """Build a minimal model/GPU environment without browser injection."""
        env = build_subprocess_env(
            base_env=base_env,
            include_configured_proxy=False,
            inject_browser_runtime=False,
            extra_allowlist=_FUNCLIP_ENV_ALLOWLIST,
        )
        # Never advertise the control-plane venv to the external model process.
        # If FUNCLIP_PYTHON is a normal venv interpreter, expose that venv and
        # lead PATH with its bin directory for subprocesses spawned by FunClip.
        env.pop("VIRTUAL_ENV", None)
        if python is not None and venv_root is not None:
            env["VIRTUAL_ENV"] = str(venv_root)
            env["PATH"] = os.pathsep.join(
                part
                for part in (str(python.parent), env.get("PATH", ""))
                if part
            )
        return env

    async def _run(self, argv: list[str], cwd: Optional[Path] = None) -> tuple[int, str, str]:
        """异步起子进程，受 funclip_timeout_seconds 兜底。"""
        root, python, entry, venv_root = self._runtime_boundary()
        actual_cwd = Path(os.path.abspath(cwd or root))
        if actual_cwd != root:
            raise RuntimeError(f"FunClip subprocess cwd 越界: {actual_cwd}")
        if len(argv) < 2 or Path(argv[0]) != python or Path(argv[1]) != entry:
            raise RuntimeError("FunClip subprocess argv 未使用已验证的解释器/入口")
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            cwd=str(root),
            env=self._subprocess_env(python=python, venv_root=venv_root),
            start_new_session=os.name == "posix",
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                communicate_bounded(proc), timeout=settings.funclip_timeout_seconds
            )
        except asyncio.CancelledError:
            await stop_process_group(proc)
            raise
        except asyncio.TimeoutError:
            await stop_process_group(proc)
            raise TimeoutError(
                f"FunClip subprocess timeout after {settings.funclip_timeout_seconds}s: "
                f"{' '.join(shlex.quote(a) for a in argv)}"
            )
        return (
            proc.returncode if proc.returncode is not None else -1,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    def _build_stage1_argv(self, input_video: str, output_dir: Path) -> list[str]:
        return [
            self._python(),
            str(self._entry()),
            "--stage", "1",
            "--file", input_video,
            "--output_dir", str(output_dir),
        ]

    def _build_stage2_argv(
        self,
        input_video: str,
        output_dir: Path,
        seg: ClipSegment,
        output_file: Path,
    ) -> list[str]:
        argv = [
            self._python(),
            str(self._entry()),
            "--stage", "2",
            "--file", input_video,
            "--output_dir", str(output_dir),
            "--output_file", str(output_file),
            "--start_ost", str(seg.start_ost_ms),
            "--end_ost", str(seg.end_ost_ms),
        ]
        if seg.dest_text:
            argv += ["--dest_text", seg.dest_text]
        return argv

    # ---------- 对外接口 ----------

    async def health(self) -> bool:
        """快速可用性检查：funclip 路径 + videoclipper.py 入口 + python 解释器。
        不真起子进程（模型加载慢，健康检查要快），只做静态校验。
        """
        try:
            self._runtime_boundary()
        except RuntimeError:
            return False
        return True

    async def _transcribe_in_run_dir(
        self, input_video: str, run_dir: Path, lang: str = "zh"
    ) -> TranscriptResult:
        """Run stage 1 inside a caller-owned, freshly created run directory."""
        # 路径一律走 os.path.abspath（不跟随 symlink，理由见 _python）——
        # 子进程 cwd 在 FunClip 根，传相对路径会被叠成嵌套路径而找不到。
        out = Path(os.path.abspath(run_dir))
        argv = self._build_stage1_argv(os.path.abspath(input_video), out)
        code, stdout, stderr = await self._run(argv, cwd=self._funclip_root())
        if code != 0:
            raise RuntimeError(
                f"FunClip stage 1 failed (code={code}). stderr=\n{stderr[-2000:]}"
            )

        # FunClip stage 1 把 SRT 写到 output_dir 下（典型名：<basename>.srt 或 res.srt）
        srt_candidates = sorted(
            candidate
            for candidate in out.glob("*.srt")
            if self._is_current_artifact(candidate, out)
        )
        if not srt_candidates:
            raise RuntimeError(
                f"FunClip stage 1 did not produce any .srt under {out} "
                f"as a non-empty regular file. stdout tail=\n{stdout[-1000:]}"
            )
        srt_path = srt_candidates[-1]
        srt_text = srt_path.read_text(encoding="utf-8")
        cues = parse_srt(srt_text)
        return TranscriptResult(
            srt_path=str(srt_path),
            cues=cues,
            full_text=" ".join(c.text for c in cues),
            meta={"stdout_tail": stdout[-500:], "lang": lang},
        )

    async def transcribe(
        self, input_video: str, output_dir: str, lang: str = "zh"
    ) -> TranscriptResult:
        run_dir, run_id = self._create_run_dir(output_dir)
        transcript = await self._transcribe_in_run_dir(input_video, run_dir, lang)
        meta = dict(transcript.meta)
        meta.update({"run_dir": str(run_dir), "run_id": run_id})
        return transcript.model_copy(update={"meta": meta})

    async def clip(self, request: ClipRequest) -> ClipResult:
        if not request.segments:
            raise ValueError("ClipRequest.segments must contain at least one segment")

        # 每次调用都原子创建全新目录。同秒并发或残留历史目录均不能共享
        # stage 1 SRT / stage 2 clip 产物。
        run_dir, run_id = self._create_run_dir(request.output_dir)
        input_video = os.path.abspath(request.input_video)

        # 先跑 stage 1 拿字幕（即便 segments 都给的是时间区间，也保留 transcript 元信息）
        transcript: Optional[TranscriptResult] = None
        try:
            transcript = await self._transcribe_in_run_dir(
                input_video, run_dir, lang=request.lang
            )
        except RuntimeError:
            # transcript 失败不阻断纯时间段剪辑（dest_text 模式下必须，调用方该感知）
            if any(seg.dest_text for seg in request.segments):
                raise
            transcript = None

        clips: list[ClipArtifact] = []
        for idx, seg in enumerate(request.segments, start=1):
            output_file = run_dir / f"clip_{idx:03d}.mp4"
            argv = self._build_stage2_argv(
                input_video, run_dir, seg, output_file
            )
            code, stdout, stderr = await self._run(argv, cwd=self._funclip_root())
            if code != 0:
                raise RuntimeError(
                    f"FunClip stage 2 failed at seg #{idx} (code={code}). "
                    f"stderr=\n{stderr[-2000:]}"
                )
            # FunClip 不按 --output_file 原样写：实际产物是 <stem>_no<N>.mp4
            # （videoclipper.py video_clip()，N=GLOBAL_COUNT，CLI 单次调用通常为 0；
            # dest_text 命中多段会 concat 进同一文件）。对外部行为不做强假设：
            # glob 限定 _no 后必有数字，再按 N 数字序排，避免 sorted 字典序错位。
            output_pattern = re.compile(
                rf"^{re.escape(output_file.stem)}_no(\d+)\.mp4$"
            )
            produced_with_index = []
            for candidate in run_dir.glob(f"{output_file.stem}_no*.mp4"):
                match = output_pattern.fullmatch(candidate.name)
                if match and self._is_current_artifact(candidate, run_dir):
                    produced_with_index.append((int(match.group(1)), candidate))
            produced = [
                candidate
                for _, candidate in sorted(produced_with_index, key=lambda item: item[0])
            ]
            if not produced:
                # FunClip 对 dest_text 无命中的 else 分支不写文件、退出码仍是 0。
                raise RuntimeError(
                    f"FunClip stage 2 produced no clip for seg #{idx}: dest_text "
                    f"{seg.dest_text!r} likely matched no speech period. "
                    f"stdout tail=\n{stdout[-800:]}"
                )
            for actual in produced:
                clips.append(
                    ClipArtifact(
                        video_path=str(actual),
                        dest_text=seg.dest_text,
                        start_ms=seg.start_ms,
                        end_ms=seg.end_ms,
                        meta={
                            "start_ost_ms": seg.start_ost_ms,
                            "end_ost_ms": seg.end_ost_ms,
                            "seg_index": idx,
                        },
                    )
                )

        return ClipResult(
            clips=clips,
            transcript=transcript,
            meta={"run_dir": str(run_dir), "run_id": run_id},
        )
