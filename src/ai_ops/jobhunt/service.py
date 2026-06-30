"""jobhunt 业务服务层（P0：简历入库）。

ingest_resume：文件 → 解析 → 落 Asset(DOCUMENT) + ResumeProfile，返回 ResumeProfile。
保持「无副作用解析 / 有副作用入库」分离：parse 在 resume_parser，落库在这里。
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.enums import AssetSource, AssetType
from ..core.models import Asset
from .models import ResumeProfile
from .resume_parser import parse_resume_file


async def ingest_resume(
    session: Session,
    file_path: str | Path,
    name: str | None = None,
    *,
    set_active: bool = True,
) -> ResumeProfile:
    """解析简历文件并落库。

    Args:
        session: 调用方管理事务（session_scope）。
        file_path: 简历文件路径（.pdf/.docx/.txt/.md）。
        name: 这份简历的标签；缺省用解析出的姓名或文件名。
        set_active: True 时把这份设为当前主用简历（其它同名候选置 is_active=False）。
    """
    p = Path(file_path)
    raw_text, structured = await parse_resume_file(p)

    # 原始文件登记为 DOCUMENT 资产（article_id 可空，独立物料）
    asset = Asset(
        article_id=None,
        asset_type=AssetType.DOCUMENT,
        source=AssetSource.USER_UPLOAD,
        local_path=str(p.resolve()),
        meta={"original_name": p.name, "kind": "resume"},
    )
    session.add(asset)
    session.flush()  # 拿 asset.id

    label = name or structured.get("name") or p.stem

    if set_active:
        # 同一时间只留一份 active 主简历
        for existing in session.scalars(
            select(ResumeProfile).where(ResumeProfile.is_active.is_(True))
        ):
            existing.is_active = False

    profile = ResumeProfile(
        name=label,
        raw_asset_id=asset.id,
        raw_text=raw_text,
        structured=structured,
        summary=structured.get("summary", ""),
        years_of_experience=structured.get("years_of_experience"),
        target_titles=structured.get("target_titles", []),
        expected_cities=structured.get("expected_cities", []),
        expected_salary_min=structured.get("expected_salary_min"),
        expected_salary_max=structured.get("expected_salary_max"),
        skills=structured.get("skills", []),
        search_keywords=structured.get("search_keywords", []),
        is_active=set_active,
    )
    session.add(profile)
    session.flush()
    return profile
