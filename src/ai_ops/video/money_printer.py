"""harry0703/MoneyPrinterTurbo 集成 wrapper（57k⭐，主力视频引擎）。

技术栈：ImageMagick + MoviePy + FFmpeg + LLM。
集成方式：
  - 优先 HTTP API（MPT 自带 FastAPI 服务，独立部署最干净）
  - fallback subprocess 调用其 Python 模块

输入：主题/关键词/可选脚本 → 输出：视频文件路径 + 封面 + 字幕。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ..config import settings
from ..core.enums import VideoEngineKind
from ..core.schemas import VideoArtifact, VideoBrief
from .base import VideoEngineBase


class MoneyPrinterEngine(VideoEngineBase):
    kind = VideoEngineKind.MONEY_PRINTER_TURBO

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
        return settings.external_mpt_path.exists()

    def _headers(self) -> dict:
        """MPT 可能要求 x-api-key（若 config.toml 配置了 app.api_key）。"""
        return {"x-api-key": settings.mpt_api_key} if settings.mpt_api_key else {}

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
        if not settings.external_mpt_path.exists():
            raise RuntimeError(f"MPT 路径不存在: {settings.external_mpt_path}")

        # MPT 提供 webui.py + main.py，CLI 接口能力受限——HTTP 模式优先
        # 这里给一个 fallback：调用其 task module
        out_dir: Path = settings.data_dir / "outputs" / "mpt-cli"
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            "python",
            "main.py",
            "--subject",
            brief.theme,
            "--output",
            str(out_dir),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(settings.external_mpt_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"MPT CLI 失败: {stderr.decode('utf-8', 'ignore')[:500]}")

        videos = sorted(out_dir.glob("*.mp4"))
        if not videos:
            raise RuntimeError("MPT CLI 未产出 mp4")
        return VideoArtifact(
            video_path=str(videos[-1]),
            duration_seconds=float(brief.duration_seconds),
            meta={"engine": "mpt-cli"},
        )
