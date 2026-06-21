"""ListenHub 云播客生成 provider（AI 播客主力，本地零算力）。

底层逻辑：
  - ListenHub（Marswave）一个 API 出成品播客：给主题/素材 → 多音色对话音频 + 文稿。
  - 异步任务：POST 建 episode 拿 episodeId → 轮询 GET 到 processStatus=success。
  - 文档建议首轮等 60s 再以 10s 间隔轮询（合成耗时较长）。

契约来源（2026-06 校对，listenhub.ai/docs/en/openapi）：
  - base:   https://api.marswave.ai/openapi
  - 鉴权:   Authorization: Bearer {LISTENHUB_API_KEY}
  - 建任务: POST /v1/podcast/episodes
            body {query, speakers:[{speakerId}], language, mode(quick|deep|debate), sources?}
  - 查任务: GET  /v1/podcast/episodes/{episodeId}
            resp {episodeId, processStatus, title, audioUrl, audioStreamUrl, scripts, credits}
"""
from __future__ import annotations

import asyncio
import time

from ..config import settings
from ..core.enums import PodcastProviderKind
from ..core.schemas import PodcastArtifact, PodcastBrief
from .base import PodcastProviderBase


class ListenHubProvider(PodcastProviderBase):
    kind = PodcastProviderKind.LISTENHUB

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {settings.listenhub_api_key}",
            "Content-Type": "application/json",
        }

    def _build_payload(self, brief: PodcastBrief) -> dict:
        payload: dict = {
            "query": brief.query,
            "speakers": [{"speakerId": s.speaker_id} for s in brief.speakers],
            "language": brief.language,
            "mode": brief.mode,
        }
        if brief.source_urls:
            payload["sources"] = [
                {"type": "url", "content": u} for u in brief.source_urls
            ]
        return payload

    async def health(self) -> bool:
        """静态校验：api key 是否配齐。不真打 API（省额度）。"""
        return bool(settings.listenhub_api_key)

    async def generate(self, brief: PodcastBrief) -> PodcastArtifact:
        if not settings.listenhub_api_key:
            raise RuntimeError("ListenHub 未配置 LISTENHUB_API_KEY")

        import httpx

        base = settings.listenhub_api_base.rstrip("/")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{base}/v1/podcast/episodes",
                json=self._build_payload(brief),
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
        episode_id = data.get("episodeId") or (data.get("data") or {}).get("episodeId")
        if not episode_id:
            raise RuntimeError(f"ListenHub 建任务无 episodeId: {data}")

        return await self._poll(episode_id)

    async def _poll(self, episode_id: str) -> PodcastArtifact:
        import httpx

        base = settings.listenhub_api_base.rstrip("/")
        url = f"{base}/v1/podcast/episodes/{episode_id}"
        deadline = time.time() + settings.listenhub_timeout_seconds

        async with httpx.AsyncClient(timeout=30) as client:
            # 文档建议首轮先等再查
            await asyncio.sleep(settings.listenhub_poll_initial_seconds)
            while time.time() < deadline:
                r = await client.get(url, headers=self._headers())
                r.raise_for_status()
                d = r.json()
                # 兼容 {data:{...}} 包裹
                d = d.get("data", d)
                status = d.get("processStatus")
                if status == "success":
                    audio_url = d.get("audioUrl")
                    local = (
                        await self._download(audio_url, episode_id)
                        if (settings.listenhub_download and audio_url)
                        else None
                    )
                    return PodcastArtifact(
                        episode_id=episode_id,
                        title=d.get("title", ""),
                        audio_url=audio_url,
                        audio_stream_url=d.get("audioStreamUrl"),
                        audio_path=local,
                        scripts=d.get("scripts", []) or [],
                        credits=d.get("credits"),
                        meta={"provider": "listenhub"},
                    )
                if status == "failed":
                    raise RuntimeError(f"ListenHub episode {episode_id} 生成失败: {d}")
                await asyncio.sleep(settings.listenhub_poll_interval_seconds)
        raise TimeoutError(
            f"ListenHub episode {episode_id} 轮询超时（{settings.listenhub_timeout_seconds}s）"
        )

    async def _download(self, url: str, episode_id: str) -> str:
        import httpx

        out_dir = settings.data_dir / "outputs" / "listenhub"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{episode_id}.mp3"
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.get(url)
            r.raise_for_status()
            out.write_bytes(r.content)
        return str(out)
