"""历史发布采集器 —— 把平台已发列表导入素材管理（喂 import_published）。

底层逻辑：
  真实历史内容来源有二，本模块统一归一到 distributor.import_published_bulk：
    1. **导出文件**（今天可用、零平台依赖）：从抖音创作者中心 / 小红书等导出帖子
       列表（CSV / JSON），import_from_csv / import_from_rows 直接回填。
    2. **在线采集**（SAU 装好后）：SAU 有读取账号已发列表的能力，collect_via_sau
       预留接入位——拿到 rows 后同样走 import_from_rows，下游零改动。

字段映射（容错）：title/url/post_id/published_at/body/content_type 多种常见列名都认。
"""
from __future__ import annotations

import csv as _csv
import json as _json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

from sqlalchemy.orm import Session

from ..core.enums import ContentType
from . import distributor

# 常见列名 → 标准字段（导出文件列名五花八门，做容错映射）
_FIELD_ALIASES = {
    "title": ["title", "标题", "作品标题", "name", "desc", "描述"],
    "platform_url": ["url", "link", "链接", "作品链接", "分享链接", "platform_url"],
    "platform_post_id": ["post_id", "id", "作品id", "aweme_id", "note_id", "platform_post_id"],
    "published_at": ["published_at", "发布时间", "create_time", "time", "date", "发表时间"],
    "body": ["body", "正文", "content", "文案", "内容"],
    "content_type": ["content_type", "类型", "type"],
}

_CT_MAP = {
    "video": ContentType.VIDEO, "视频": ContentType.VIDEO, "短剧": ContentType.VIDEO,
    "image_text": ContentType.IMAGE_TEXT, "图文": ContentType.IMAGE_TEXT, "image": ContentType.IMAGE_TEXT,
    "long_article": ContentType.LONG_ARTICLE, "文章": ContentType.LONG_ARTICLE, "博客": ContentType.LONG_ARTICLE,
    "audio": ContentType.AUDIO, "音频": ContentType.AUDIO, "播客": ContentType.AUDIO,
}


def _pick(row: dict, field: str) -> Optional[str]:
    for alias in _FIELD_ALIASES[field]:
        for k in row:
            if k.strip().lower() == alias.lower() and str(row[k]).strip():
                return str(row[k]).strip()
    return None


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _normalize(row: dict) -> dict:
    """一行导出记录 → import_published_post 的 kwargs。"""
    ct_raw = (_pick(row, "content_type") or "").lower()
    ct = _CT_MAP.get(ct_raw, ContentType.IMAGE_TEXT)
    return {
        "title": _pick(row, "title") or "(历史无标题)",
        "content_type": ct,
        "body": _pick(row, "body") or "",
        "platform_url": _pick(row, "platform_url"),
        "platform_post_id": _pick(row, "platform_post_id"),
        "published_at": _parse_dt(_pick(row, "published_at")),
    }


def import_from_rows(session: Session, account_id: int, rows: Iterable[dict]):
    """从已解析的行（dict 列表）回填历史发布。返回创建/命中的 PublishJob 列表。"""
    posts = [_normalize(r) for r in rows]
    return distributor.import_published_bulk(session, account_id, posts)


def import_from_csv(session: Session, account_id: int, csv_path: str | Path):
    """从 CSV 导出文件回填（首行表头，列名容错）。"""
    path = Path(csv_path)
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(_csv.DictReader(f))
    return import_from_rows(session, account_id, rows)


def import_from_json(session: Session, account_id: int, json_path: str | Path):
    """从 JSON 导出文件回填（数组，或 {data:[...]} / {list:[...]} 包裹）。"""
    data = _json.loads(Path(json_path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("data") or data.get("list") or data.get("items") or []
    return import_from_rows(session, account_id, data)


def collect_via_sau(session: Session, account_id: int, rows: Sequence[dict]):
    """在线采集接入位：SAU 装好后由其读取账号已发列表，拿到 rows 后走同一回填路径。

    目前 SAU 未装/本机不可上传，先把 rows 透传（调用方拿到 SAU 输出后调本函数）。
    """
    return import_from_rows(session, account_id, rows)
