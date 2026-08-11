"""harry0703/MoneyPrinterTurbo 集成 wrapper（可选视频引擎）。

技术栈：ImageMagick + MoviePy + FFmpeg + LLM。
集成方式：
  - 优先 HTTP API（MPT 自带 FastAPI 服务，独立部署最干净）
  - fallback subprocess 调用其 Python 模块

输入：主题/关键词/可选脚本 → 输出：视频文件路径 + 封面 + 字幕。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile

from ..config import settings
from ..core.enums import VideoEngineKind
from ..core.schemas import VideoArtifact, VideoBrief
from ..runtime.browser_engine import build_subprocess_env
from ..runtime.subprocess import communicate_bounded, stop_process_group
from .base import VideoEngineBase


class MoneyPrinterEngine(VideoEngineBase):
    kind = VideoEngineKind.MONEY_PRINTER_TURBO

    def _mpt_root(self) -> Path:
        return Path(os.path.abspath(settings.external_mpt_path))

    def _cli_boundary(self) -> tuple[Path, Path, Path, Path]:
        """Validate the local MPT repository, entrypoint and isolated Python.

        The interpreter path is checked lexically rather than resolved because
        a normal venv ``bin/python`` is itself a symlink.  It must still be
        configured beneath the MPT repository so a typo cannot execute an
        arbitrary PATH/control-plane interpreter.
        """
        root = self._mpt_root()
        if not root.is_dir():
            raise RuntimeError(f"MPT 路径不存在或不是目录: {root}")

        entry = root / "main.py"
        if not entry.is_file() or entry.is_symlink():
            raise RuntimeError(f"MPT CLI 入口无效: {entry}")

        configured_python = settings.mpt_python.strip()
        if not configured_python:
            raise RuntimeError(
                "MPT_PYTHON 未配置；CLI 模式必须使用 MPT 仓库内的独立 venv 解释器"
            )
        python = Path(os.path.abspath(os.path.expanduser(configured_python)))
        try:
            python.relative_to(root)
        except ValueError as exc:
            raise RuntimeError(
                f"MPT_PYTHON 必须位于 EXTERNAL_MPT_PATH 内: {python}"
            ) from exc
        if not python.is_file() or not os.access(python, os.X_OK):
            raise RuntimeError(f"MPT_PYTHON 不存在或不可执行: {python}")
        venv_root = python.parent.parent
        if not (venv_root / "pyvenv.cfg").is_file():
            raise RuntimeError(
                f"MPT_PYTHON 必须来自独立 venv（缺少 {venv_root / 'pyvenv.cfg'}）"
            )
        return root, python, entry, venv_root

    async def render(self, brief: VideoBrief) -> VideoArtifact:
        if settings.external_mpt_url:
            return await self._render_via_http(brief)
        return await self._render_via_cli(brief)

    async def health(self) -> bool:
        if settings.external_mpt_url:
            import httpx
            try:
                async with httpx.AsyncClient(timeout=5, headers=self._headers()) as client:
                    r = await client.get(f"{settings.external_mpt_url}/ping")
                return r.status_code == 200
            except Exception:
                return False
        try:
            self._cli_boundary()
        except RuntimeError:
            return False
        return True

    def _headers(self) -> dict:
        """MPT 可能要求 x-api-key（若 config.toml 配置了 app.api_key）。"""
        return {"x-api-key": settings.mpt_api_key} if settings.mpt_api_key else {}

    @staticmethod
    def _run_videos(run_dir: Path) -> list[Path]:
        """Return only non-empty, regular mp4 files contained by one run dir."""
        resolved_root = run_dir.resolve(strict=True)
        videos: list[Path] = []
        for candidate in run_dir.rglob("*.mp4"):
            # A tool-controlled symlink could otherwise make an old artifact
            # outside this run look like fresh output.  The run directory itself
            # is unique and starts empty, so contained regular files can only
            # have appeared during this invocation.
            if candidate.is_symlink() or not candidate.is_file():
                continue
            try:
                resolved = candidate.resolve(strict=True)
                resolved.relative_to(resolved_root)
                if resolved.stat().st_size <= 0:
                    continue
            except (OSError, ValueError):
                continue
            videos.append(resolved)
        return videos

    async def _render_via_http(self, brief: VideoBrief) -> VideoArtifact:
        """调 MPT 的 REST API。校对自上游 app/controllers/v1/video.py + v1/base.py。

        前缀：/api/v1（v1/base.py: router.prefix = "/api/v1"）
        路由：POST /api/v1/videos · GET /api/v1/tasks/{task_id}
        字段：app/models/schema.py VideoParams
        """
        import httpx

        payload = {
            "video_subject": brief.theme,
            "video_script": brief.script or "",
            "video_terms": brief.keywords,
            "video_aspect": "9:16" if "1920" in brief.resolution else "16:9",
            "voice_name": brief.voice or "zh-CN-XiaoxiaoNeural-Female",
            "bgm_type": "random" if not brief.bgm else "file",
            "bgm_file": brief.bgm or "",
            "subtitle_enabled": True,
        }
        async with httpx.AsyncClient(timeout=30, headers=self._headers()) as client:
            resp = await client.post(f"{settings.external_mpt_url}/api/v1/videos", json=payload)
            resp.raise_for_status()
            task_id = resp.json()["data"]["task_id"]

        # 轮询直到完成
        output_dir = settings.data_dir / "outputs" / "mpt" / task_id
        async with httpx.AsyncClient(timeout=30, headers=self._headers()) as client:
            for _ in range(360):  # 最长 30 分钟，5s 一次
                await asyncio.sleep(5)
                r = await client.get(f"{settings.external_mpt_url}/api/v1/tasks/{task_id}")
                data = r.json()["data"]
                if data.get("state") == "complete":
                    video_path = data.get("videos", [None])[0]
                    return VideoArtifact(
                        video_path=video_path or str(output_dir / "final.mp4"),
                        cover_path=data.get("combined_videos", [None])[0],
                        subtitle_path=data.get("subtitle_path"),
                        duration_seconds=float(data.get("duration", brief.duration_seconds)),
                        meta={"task_id": task_id, "engine": "mpt-http"},
                    )
                if data.get("state") == "failed":
                    raise RuntimeError(f"MPT 任务失败: {data.get('error')}")
        raise TimeoutError(f"MPT 任务 {task_id} 超时")

    async def _render_via_cli(self, brief: VideoBrief) -> VideoArtifact:
        """subprocess 模式（MPT 作为本地项目）。"""
        root, python, entry, venv_root = self._cli_boundary()

        # MPT 提供 webui.py + main.py，CLI 接口能力受限——HTTP 模式优先
        # 这里给一个 fallback：调用其 task module
        # The child runs with cwd=MPT root, so the output path passed in argv
        # must be absolute even when DATA_DIR is configured relatively.
        output_root = Path(
            os.path.abspath(settings.data_dir / "outputs" / "mpt-cli")
        )
        output_root.mkdir(parents=True, exist_ok=True)
        # Never share an output directory across runs.  A shared glob can return
        # a stale or concurrent task's mp4 when the current command exits 0
        # without producing anything.  mkdtemp creates an unpredictable 0700
        # directory atomically beneath the configured output root.
        run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=output_root))
        cmd = [
            str(python),
            str(entry),
            "--subject",
            brief.theme,
            "--output",
            str(run_dir),
        ]
        child_env = build_subprocess_env(
            include_configured_proxy=False,
            inject_browser_runtime=False,
        )
        # Prevent the parent control-plane venv from leaking into MPT.  The
        # explicit MPT venv also leads PATH for any tools MPT itself spawns.
        child_env["VIRTUAL_ENV"] = str(venv_root)
        child_env["PATH"] = os.pathsep.join(
            part
            for part in (str(python.parent), child_env.get("PATH", ""))
            if part
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(root),
            env=child_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        try:
            _, stderr = await asyncio.wait_for(
                communicate_bounded(proc),
                timeout=float(settings.mpt_cli_timeout_seconds),
            )
        except asyncio.CancelledError:
            await stop_process_group(proc)
            raise
        except TimeoutError as exc:
            await stop_process_group(proc)
            raise TimeoutError(
                f"MPT CLI 超时（>{settings.mpt_cli_timeout_seconds}s）"
            ) from exc
        if proc.returncode != 0:
            raise RuntimeError(f"MPT CLI 失败: {stderr.decode('utf-8', 'ignore')[:500]}")

        videos = sorted(
            self._run_videos(run_dir),
            key=lambda path: (path.stat().st_mtime_ns, str(path)),
        )
        if not videos:
            raise RuntimeError("MPT CLI 本轮未产出非空 mp4")
        return VideoArtifact(
            video_path=str(videos[-1]),
            duration_seconds=float(brief.duration_seconds),
            meta={"engine": "mpt-cli", "run_dir": str(run_dir)},
        )
