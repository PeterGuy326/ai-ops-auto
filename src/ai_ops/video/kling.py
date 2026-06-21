"""可灵 Kling 云视频生成引擎（AI 短剧主力，本地零算力）。

底层逻辑：
  - 短剧需要「真剧情/角色/分镜」，本地 MoneyPrinterTurbo（素材库+口播）做不到，
    且本地无 GPU 算力。可灵走云 API，按量付费，零本地算力。
  - Kling 鉴权是 JWT(HS256)：payload {iss=access_key, exp=now+1800, nbf=now-5}，
    用 secret_key 签名。token 30min 过期 → 每次请求现签，不缓存。
  - 文生视频是异步任务：POST 建任务拿 task_id → 轮询 GET 到 task_status=succeed。

为什么手写 JWT 而不引入 PyJWT：
  HS256 = base64url(header).base64url(payload) 用 HMAC-SHA256 签名，stdlib
  hmac/hashlib/base64 足矣。主项目环境精简，零新增依赖是对的（owner 意识）。

契约来源（2026-06 校对）：
  - 鉴权：kling.ai/document-api/apiReference/commonInfo
  - 文生视频：klingai.com/document-api/apiReference/model/textToVideo
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

from ..config import settings
from ..core.enums import VideoEngineKind
from ..core.schemas import VideoArtifact, VideoBrief
from .base import VideoEngineBase


def _b64url(raw: bytes) -> str:
    """base64url 无填充（JWT 规范）。"""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def encode_jwt_hs256(access_key: str, secret_key: str, *, now: int | None = None) -> str:
    """生成 Kling 要求的 JWT(HS256)。

    now 可注入便于测试（避免依赖墙上时钟）。
    """
    ts = int(now if now is not None else time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"iss": access_key, "exp": ts + 1800, "nbf": ts - 5}
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(payload, separators=(",", ":")).encode())
    )
    sig = hmac.new(secret_key.encode(), signing_input.encode(), hashlib.sha256).digest()
    return signing_input + "." + _b64url(sig)


class KlingEngine(VideoEngineBase):
    kind = VideoEngineKind.KLING

    def _auth_header(self) -> dict:
        token = encode_jwt_hs256(settings.kling_access_key, settings.kling_secret_key)
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _aspect_ratio(self, brief: VideoBrief) -> str:
        # 竖屏短剧默认 9:16；横屏 16:9
        return "9:16" if "1920" in brief.resolution or brief.resolution.startswith("1080x19") else "16:9"

    async def health(self) -> bool:
        """静态校验：access/secret key 是否配齐。不真打 API（省额度）。"""
        return bool(settings.kling_access_key and settings.kling_secret_key)

    async def render(self, brief: VideoBrief) -> VideoArtifact:
        if not (settings.kling_access_key and settings.kling_secret_key):
            raise RuntimeError("Kling 未配置 KLING_ACCESS_KEY / KLING_SECRET_KEY")

        import httpx

        base = settings.kling_api_base.rstrip("/")
        # 时长：Kling 取 "5"/"10" 字符串（秒）
        duration = "10" if brief.duration_seconds >= 10 else "5"
        payload = {
            "model_name": settings.kling_model,
            "prompt": brief.script or brief.theme,
            "negative_prompt": brief.extra.get("negative_prompt", ""),
            "duration": duration,
            "aspect_ratio": self._aspect_ratio(brief),
            "mode": settings.kling_mode,
        }
        if brief.extra.get("cfg_scale") is not None:
            payload["cfg_scale"] = brief.extra["cfg_scale"]
        if brief.extra.get("external_task_id"):
            payload["external_task_id"] = brief.extra["external_task_id"]

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{base}/v1/videos/text2video", json=payload, headers=self._auth_header()
            )
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") not in (0, None):
            raise RuntimeError(f"Kling 建任务失败: code={data.get('code')} msg={data.get('message')}")
        task_id = data["data"]["task_id"]

        artifact = await self._poll(task_id, brief)
        return artifact

    async def _poll(self, task_id: str, brief: VideoBrief) -> VideoArtifact:
        import httpx

        base = settings.kling_api_base.rstrip("/")
        deadline = time.time() + settings.kling_timeout_seconds
        url = f"{base}/v1/videos/text2video/{task_id}"

        async with httpx.AsyncClient(timeout=30) as client:
            while time.time() < deadline:
                await asyncio.sleep(settings.kling_poll_interval_seconds)
                r = await client.get(url, headers=self._auth_header())
                r.raise_for_status()
                d = r.json().get("data", {})
                status = d.get("task_status")
                if status == "succeed":
                    videos = (d.get("task_result") or {}).get("videos") or []
                    if not videos:
                        raise RuntimeError(f"Kling task {task_id} succeed 但无 video URL")
                    video_url = videos[0]["url"]
                    duration = float(videos[0].get("duration", brief.duration_seconds) or brief.duration_seconds)
                    local = await self._download(video_url, task_id) if settings.kling_download else None
                    return VideoArtifact(
                        video_path=local or video_url,
                        duration_seconds=duration,
                        meta={
                            "engine": "kling",
                            "task_id": task_id,
                            "remote_url": video_url,
                            "transient_warning": "Kling 生成物 30 天后清理，需及时转存",
                        },
                    )
                if status == "failed":
                    raise RuntimeError(f"Kling task {task_id} 失败: {d.get('task_status_msg')}")
        raise TimeoutError(f"Kling task {task_id} 轮询超时（{settings.kling_timeout_seconds}s）")

    async def _download(self, url: str, task_id: str) -> str:
        """成片下载到本地（发布器要本地文件；Kling 远端有时效）。"""
        import httpx

        out_dir = settings.data_dir / "outputs" / "kling"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{task_id}.mp4"
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.get(url)
            r.raise_for_status()
            out.write_bytes(r.content)
        return str(out)
