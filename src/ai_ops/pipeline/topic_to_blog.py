"""主题 → AI 长文 → 自有博客（GitHub Pages / Hexo）的场景编排层。

底层逻辑：
  - 文字类（知乎/头条/公众号…）已有发布器，本流水线补的是「博客」这条自有
    阵地链路：AI 生成长文 → frontmatter + Markdown → Hexo 仓库 → 构建 → push。
  - 自有博客没有反爬/风控问题，只需「写文件 + 跑构建 + git push」，所以这条链
    路天然可以本地端到端真跑（dry_run 关掉即真发）。

边界（刻意为之）：
  本流水线直接调 GitHubPagesPublisher.publish——博客是「单账号 + 自有阵地」，
  不需要走多账号风控扇出（那是抖音/小红书的事）。真发布的幂等/重试由 git 自身
  与 publisher 内部 git_publish 处理。

可注入：generator / publisher 均可替换，便于本地用 fake LLM + 临时 git 仓库验证。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from ..content.generator import ContentGenerator
from ..core.enums import ContentType
from ..core.schemas import PublishContent, PublishResult
from ..publishers.github_pages import GitHubPagesPublisher


class BlogRequest(BaseModel):
    """AI 博客编排入参。"""
    topic_name: str
    keywords: list[str] = Field(default_factory=list)
    persona: dict = Field(default_factory=dict)
    # 博客专属 frontmatter
    categories: list[str] = Field(default_factory=list)
    description: str = ""
    cover: str = ""
    # 专题层 prompt（prompts/topics/{slug}.md），为空走通用
    topic_slug: str = ""


class BlogResult(BaseModel):
    """AI 博客编排产物。"""
    success: bool
    title: str
    body_chars: int = 0
    tags: list[str] = Field(default_factory=list)
    article_url: str | None = None
    error: str | None = None
    raw: dict = Field(default_factory=dict)


class TopicToBlogPipeline:
    """把「一个主题」编排成「一篇已发（或预览）的博客文章」。"""

    def __init__(
        self,
        generator: ContentGenerator | None = None,
        publisher: GitHubPagesPublisher | None = None,
    ) -> None:
        # generator 默认按 config 选 LLM driver；publisher 默认 Hexo 博客
        self.generator = generator or ContentGenerator()
        self.publisher = publisher or GitHubPagesPublisher()

    async def run(self, request: BlogRequest) -> BlogResult:
        """主题 → 生成 → 发布（dry_run 与否由 settings.github_pages_dry_run 决定）。"""
        gen = await self.generator.generate(
            topic_name=request.topic_name,
            keywords=request.keywords,
            persona=request.persona,
            platform_hint="博客",
            topic_slug=request.topic_slug,
        )

        content = self._to_publish_content(gen, request)
        # account_id 对自有博客无实义（git 用本地凭证），固定 0
        result: PublishResult = await self.publisher.publish(
            account_id=0, credential={}, content=content
        )

        return BlogResult(
            success=result.success,
            title=content.title,
            body_chars=len(content.body),
            tags=content.tags,
            article_url=result.platform_url,
            error=result.error,
            raw=result.raw_response,
        )

    def _to_publish_content(self, gen: dict, request: BlogRequest) -> PublishContent:
        """生成结果 → 平台无关 PublishContent（LONG_ARTICLE）。

        extra 里的 categories/description/cover 会被 GitHubPagesPublisher 写进
        frontmatter（见其 _extra_frontmatter / categories 处理）。
        """
        extra: dict = {}
        if request.categories:
            extra["categories"] = request.categories
        if request.description:
            extra["description"] = request.description
        if request.cover:
            extra["cover"] = request.cover

        return PublishContent(
            title=gen.get("title") or request.topic_name,
            body=gen.get("body", ""),
            content_type=ContentType.LONG_ARTICLE,
            tags=gen.get("tags", []) or request.keywords,
            extra=extra,
        )
