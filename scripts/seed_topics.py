"""幂等种 3 个内置专题：dws / 软考 / 篮球vlog。

- 已存在（按 name 匹配）→ 补齐缺失字段（category/keywords/target_platforms），已有字段不覆盖
- 不存在 → 全量创建
"""
from __future__ import annotations

from sqlalchemy import select

from ai_ops.core.db import session_scope
from ai_ops.core.enums import Platform
from ai_ops.core.models import Topic


# (name, category, keywords, target_platforms, notes)
SEEDS = [
    (
        "dws",
        "tech",
        ["钉钉", "AI表格", "审批", "听记", "IM机器人"],
        [Platform.XIAOHONGSHU, Platform.ZHIHU, Platform.WECHAT_MP],
        "钉钉数字化办公 / Digital Workspace 产品矩阵：AI表格、审批、听记、IM机器人等",
    ),
    (
        "软考",
        "exam",
        ["系统架构师", "软考", "备考", "高项", "中项"],
        [Platform.ZHIHU, Platform.XIAOHONGSHU],
        "软考备考赛道：高项 / 中项 / 系统架构师 / 经验贴 / 真题",
    ),
    (
        "篮球vlog",
        "sports",
        ["NBA", "CBA", "球评", "集锦", "装备"],
        [Platform.XIAOHONGSHU, Platform.WECHAT_VIDEO, Platform.BILIBILI],
        "篮球内容：NBA/CBA 赛后球评、集锦剪辑、装备测评",
    ),
]


def seed() -> dict:
    """执行 seed，返回 {created: [...], patched: [...], untouched: [...]}。"""
    result = {"created": [], "patched": [], "untouched": []}

    with session_scope() as s:
        for name, category, keywords, target_platforms, notes in SEEDS:
            existing = s.execute(select(Topic).where(Topic.name == name)).scalar_one_or_none()
            target_platforms_str = [p.value for p in target_platforms]

            if existing is None:
                t = Topic(
                    name=name,
                    category=category,
                    keywords=keywords,
                    persona={},
                    target_platforms=target_platforms_str,
                    notes=notes,
                )
                s.add(t)
                result["created"].append(name)
                continue

            # 幂等 patch：仅补齐空字段，不覆盖
            patched_fields = []
            if not existing.category or existing.category == "general":
                existing.category = category
                patched_fields.append("category")
            if not existing.keywords:
                existing.keywords = keywords
                patched_fields.append("keywords")
            if not existing.target_platforms:
                existing.target_platforms = target_platforms_str
                patched_fields.append("target_platforms")
            if not existing.notes:
                existing.notes = notes
                patched_fields.append("notes")

            if patched_fields:
                result["patched"].append({"name": name, "fields": patched_fields})
            else:
                result["untouched"].append(name)

    return result


if __name__ == "__main__":
    r = seed()
    print("OK: seed_topics")
    print(f"  created:   {r['created'] or '(none)'}")
    print(f"  patched:   {r['patched'] or '(none)'}")
    print(f"  untouched: {r['untouched'] or '(none)'}")
