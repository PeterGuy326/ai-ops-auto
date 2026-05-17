"""自有博客（GitHub Pages）发布器 — Hexo / Jekyll / Hugo 通用。

底层逻辑：
  - 自有博客天然没有"反爬"问题，只需要"写文件 + 跑构建 + git push"。
  - 第一版聚焦 Hexo（用户当前博客 PeterGuy326.github.io 用 Hexo + Butterfly）。
  - Jekyll / Hugo 留扩展点，frontmatter 格式略有差异。

闭环：
  PublishContent → frontmatter + markdown → source/_posts/<slug>.md
              → 拷贝图片到 source/img/<slug>/
              → 跑 hexo generate
              → git add/commit/push
              → 返回 platform_url = base_url/<slug>/
"""
from __future__ import annotations

import asyncio
import re
import shutil
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from ..config import settings
from ..core.enums import AccountHealth, ContentType, Platform, PublisherKind
from ..core.schemas import PublishContent, PublishResult
from .base import PublisherBase


# 友好的 frontmatter — 兼容 Hexo Butterfly 主题（用户当前用的）
HEXO_FRONTMATTER_TEMPLATE = """---
title: {title}
date: {date}
tags:
{tags}
categories:
{categories}
{extra_fields}---

"""


def _slugify(title: str) -> str:
    """中英文混合标题 → URL 友好 slug。

    简化版：保留 ASCII 字母数字 + 中划线，其它替换为空（中文会被去掉）。
    如果剥光是空的，回退到时间戳。
    """
    s = re.sub(r"[^\w一-鿿-]+", "-", title)  # 保留中文 + word
    s = re.sub(r"-+", "-", s).strip("-").lower()
    if not s:
        s = "post-" + datetime.now().strftime("%Y%m%d%H%M%S")
    return s[:80]  # 限长


def _yaml_list(items: list[str]) -> str:
    if not items:
        return "  []"
    return "\n".join(f"  - {it}" for it in items)


class GitHubPagesPublisher(PublisherBase):
    """Hexo / Jekyll / Hugo 静态博客发布器。

    credential 字段可为空（git push 用本地 git credential，不需要业务侧管理）。
    """
    platform = Platform.GITHUB_PAGES
    kind = PublisherKind.HEXO

    async def login(self, account_id: int, credential: dict) -> bool:
        """检查本地 git 是否能 push（探活式 git ls-remote）。"""
        repo = settings.github_pages_path
        if not (repo / ".git").exists():
            return False
        proc = await asyncio.create_subprocess_exec(
            "git", "ls-remote", "--exit-code", "origin", "HEAD",
            cwd=str(repo),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0

    async def health_check(self, account_id: int, credential: dict) -> AccountHealth:
        return AccountHealth.HEALTHY if await self.login(account_id, credential) else AccountHealth.EXPIRED

    async def publish(
        self,
        account_id: int,
        credential: dict,
        content: PublishContent,
    ) -> PublishResult:
        repo = settings.github_pages_path
        if not repo.exists():
            return PublishResult(success=False, error=f"博客仓库不存在: {repo}")
        if not (repo / ".git").exists():
            return PublishResult(success=False, error=f"{repo} 不是 git 仓库")
        if settings.github_pages_engine != "hexo":
            return PublishResult(
                success=False,
                error=f"暂仅支持 hexo，当前 engine={settings.github_pages_engine}（Jekyll/Hugo 留扩展点）",
            )

        slug = _slugify(content.title)
        posts_dir = repo / settings.github_pages_posts_dir
        post_path = posts_dir / f"{slug}.md"

        # 1. 拷贝图片到 source/img/<slug>/，正文里的图片引用统一替换成 /img/<slug>/<name>
        body = content.body or "" if settings.github_pages_dry_run else \
            await self._materialize_images(repo, slug, content)

        # 2. 渲染 frontmatter + markdown
        frontmatter = HEXO_FRONTMATTER_TEMPLATE.format(
            title=content.title.replace('"', '\\"'),
            date=datetime.now().isoformat(timespec="seconds"),
            tags=_yaml_list(content.tags),
            categories=_yaml_list(content.extra.get("categories", []) if content.extra else []),
            extra_fields=self._extra_frontmatter(content),
        )
        rendered = frontmatter + body

        # dry_run：不写文件 / 不构建 / 不 push，只返回预览
        if settings.github_pages_dry_run:
            return PublishResult(
                success=True,
                platform_post_id=slug,
                platform_url=f"{settings.github_pages_base_url.rstrip('/')}/{quote(slug, safe='')}/  [DRY_RUN]",
                raw_response={
                    "dry_run": True,
                    "slug": slug,
                    "would_write_to": str(post_path),
                    "preview_first_500": rendered[:500],
                    "preview_total_bytes": len(rendered.encode("utf-8")),
                    "images_count": len(content.images),
                },
            )

        posts_dir.mkdir(parents=True, exist_ok=True)
        post_path.write_text(rendered, encoding="utf-8")

        # 3. 跑构建（pnpm hexo generate）
        build_ok, build_log = await self._run_shell(settings.github_pages_build_cmd, cwd=repo)
        if not build_ok:
            return PublishResult(success=False, error=f"hexo generate 失败: {build_log[:500]}")

        # 4. git add/commit/push
        push_ok, push_log = await self._git_publish(repo, slug, content.title)
        if not push_ok:
            return PublishResult(success=False, error=f"git push 失败: {push_log[:500]}")

        # 中文 slug 必须 percent-encode，否则浏览器/CDN/转链层会 400 Bad Request
        article_url = f"{settings.github_pages_base_url.rstrip('/')}/{quote(slug, safe='')}/"
        return PublishResult(
            success=True,
            platform_post_id=slug,
            platform_url=article_url,
            raw_response={
                "post_path": str(post_path),
                "slug_raw": slug,
                "slug_encoded": quote(slug, safe=""),
                "build_log": build_log[-500:],
            },
        )

    # ---------------- 内部 ----------------

    async def _materialize_images(self, repo: Path, slug: str, content: PublishContent) -> str:
        """图片落到 source/img/<slug>/，正文里替换路径。"""
        body = content.body or ""
        if not content.images:
            return body

        img_root = repo / settings.github_pages_images_dir / slug
        img_root.mkdir(parents=True, exist_ok=True)

        for src in content.images:
            src_p = Path(src)
            if not src_p.exists():
                continue
            dst = img_root / src_p.name
            shutil.copyfile(src_p, dst)
            site_path = f"/{settings.github_pages_images_dir.replace('source/', '').strip('/')}/{slug}/{src_p.name}"
            # 在正文末尾追加图片（如果正文没引用到）
            if src_p.name not in body and src not in body:
                body += f"\n\n![{src_p.stem}]({site_path})"
        return body

    def _extra_frontmatter(self, content: PublishContent) -> str:
        """额外 frontmatter（cover / top_img 等，从 content.extra 取）。"""
        extra = content.extra or {}
        lines = []
        for key in ("cover", "top_img", "description", "keywords"):
            if extra.get(key):
                lines.append(f"{key}: {extra[key]}")
        return ("\n".join(lines) + "\n") if lines else ""

    async def _run_shell(self, cmd: str, cwd: Path) -> tuple[bool, str]:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        return proc.returncode == 0, stdout.decode("utf-8", "ignore")

    async def _git_publish(self, repo: Path, slug: str, title: str) -> tuple[bool, str]:
        commit_msg = f"post: {title} ({slug})"
        # 三步分别跑，便于精准定位失败
        for step in (
            ["git", "add", "-A"],
            ["git", "commit", "-m", commit_msg],
            ["git", "push"],
        ):
            proc = await asyncio.create_subprocess_exec(
                *step, cwd=str(repo),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            text = out.decode("utf-8", "ignore")
            # commit 在"没有变更"时返回非 0，但不算失败
            if proc.returncode != 0 and step[1] == "commit" and "nothing to commit" in text:
                continue
            if proc.returncode != 0:
                return False, f"{' '.join(step)} 失败:\n{text}"
        return True, "ok"
