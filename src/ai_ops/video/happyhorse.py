"""悟空开放平台 HappyHorse 云视频生成引擎（AI 短剧主力，内网，本地零算力）。

底层逻辑：
  - HappyHorse（阿里 ATH「欢乐马」）文生视频，挂在 idealab AI Studio / 悟空开放平台。
    用专用子AK（aiopsauto）鉴权，按额度计费，本地零 GPU、无封控。
  - 协议 = idealab 网关「视频任务」异步接口（实测确认，2026-06）：
      建任务  POST {jobs_url}            头 Authorization: Bearer
              body {model, extendParams:{input:{prompt}, parameters:{resolution,ratio,duration}}}
              → {id, status:"running", object:"video.generation.job"}
      轮询    POST {jobs_url}/{job_id}    body {model}
              → 进行中 {status:"running"}；完成 {generations:[{id, url}]}（url=mp4，有时效）

可插拔：实现 VideoEngineBase，接进 script_to_drama 后剧本→视频→切片→发布全自动。
模型可换 sora-0502 / veo-3.0-generate-preview（同端点，extendParams 形状各异）。
"""
from __future__ import annotations

import asyncio
import time

from ..config import settings
from ..core.enums import VideoEngineKind
from ..core.schemas import VideoArtifact, VideoBrief
from .base import VideoEngineBase


class HappyHorseEngine(VideoEngineBase):
    kind = VideoEngineKind.KLING  # 云视频生成同档；如需独立枚举可加 HAPPYHORSE

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {settings.wukong_api_key}",
            "Content-Type": "application/json",
        }

    def _ratio(self, brief: VideoBrief) -> str:
        if brief.resolution and "x" in brief.resolution:
            w, _, h = brief.resolution.partition("x")
            try:
                return "9:16" if int(w) < int(h) else "16:9"
            except ValueError:
                pass
        return settings.wukong_video_ratio

    async def health(self) -> bool:
        """静态校验：key 是否配齐。不真打 API（省额度）。"""
        return bool(settings.wukong_api_key)

    async def render(self, brief: VideoBrief) -> VideoArtifact:
        if not settings.wukong_api_key:
            raise RuntimeError("HappyHorse 未配置 WUKONG_API_KEY")

        import httpx

        model = brief.extra.get("model", settings.wukong_video_model)
        duration = max(3, min(15, int(brief.duration_seconds or 3)))
        payload = {
            "model": model,
            "extendParams": {
                "input": {"prompt": brief.script or brief.theme},
                "parameters": {
                    "resolution": brief.extra.get("resolution", settings.wukong_video_resolution),
                    "ratio": self._ratio(brief),
                    "duration": duration,
                },
            },
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                settings.wukong_video_jobs_url, headers=self._headers(), json=payload
            )
            resp.raise_for_status()
            data = resp.json()
        job_id = data.get("id") or (data.get("generations") or [{}])[0].get("id")
        if not job_id:
            raise RuntimeError(f"HappyHorse 建任务无 job id: {str(data)[:300]}")

        return await self._poll(job_id, model, duration)

    async def _poll(self, job_id: str, model: str, duration: int) -> VideoArtifact:
        import httpx

        url = f"{settings.wukong_video_jobs_url.rstrip('/')}/{job_id}"
        deadline = time.time() + settings.wukong_timeout_seconds
        async with httpx.AsyncClient(timeout=30) as client:
            while time.time() < deadline:
                await asyncio.sleep(settings.wukong_poll_interval_seconds)
                r = await client.post(url, headers=self._headers(), json={"model": model})
                r.raise_for_status()
                d = r.json()
                gens = d.get("generations") or []
                video_url = gens[0].get("url") if gens else None
                if video_url:
                    local = await self._download(video_url, job_id) if settings.wukong_download else None
                    return VideoArtifact(
                        video_path=local or video_url,
                        duration_seconds=float(duration),
                        meta={
                            "engine": "happyhorse",
                            "model": model,
                            "job_id": job_id,
                            "remote_url": video_url,
                            "transient_warning": "url 有时效，需及时转存",
                        },
                    )
                status = (d.get("status") or "").lower()
                if status in ("failed", "error", "canceled"):
                    raise RuntimeError(f"HappyHorse job {job_id} 失败: {str(d)[:300]}")
        raise TimeoutError(f"HappyHorse job {job_id} 轮询超时（{settings.wukong_timeout_seconds}s）")

    async def _download(self, url: str, job_id: str) -> str:
        import httpx

        out_dir = settings.data_dir / "outputs" / "happyhorse"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{job_id}.mp4"
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.get(url)
            r.raise_for_status()
            out.write_bytes(r.content)
        return str(out)
