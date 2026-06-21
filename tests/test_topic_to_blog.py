"""TopicToBlogPipeline 单测 —— AI 博客全链路本地验证。

注入 fake LLM driver + 临时 git 仓库 + dry_run，端到端验证：
  1. 主题 → 生成 → frontmatter+Markdown → 博客发布（dry_run 预览）
  2. tags / categories / description 正确进 frontmatter
  3. 标题透传、中文 slug 正确 percent-encode 进 URL
  4. publisher.publish 失败时 BlogResult.success=False 且带 error
不依赖真实 LLM、不依赖真实博客仓库的网络 push。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_ops.config import settings
from ai_ops.content.generator import ContentGenerator, LLMDriver
from ai_ops.core.schemas import PublishResult
from ai_ops.pipeline.topic_to_blog import BlogRequest, TopicToBlogPipeline
from ai_ops.publishers.github_pages import GitHubPagesPublisher


class FakeLLM(LLMDriver):
    async def complete(self, system: str, user: str, **kwargs) -> str:
        return (
            "# 我用 AI 把运维日报自动化了\n\n"
            "过去每天手动整理三平台数据，现在一条流水线全自动。\n\n"
            "## 三个抓手\n1. 内容生成\n2. 自动剪辑\n3. 多平台分发\n"
        )


@pytest.fixture
def blog_repo(tmp_path: Path):
    """造一个最小 hexo-like git 仓库，并把 settings 指过去 + 开 dry_run。"""
    import subprocess

    repo = tmp_path / "blog"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.co"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)

    old_path = settings.github_pages_path
    old_dry = settings.github_pages_dry_run
    settings.github_pages_path = repo
    settings.github_pages_dry_run = True
    yield repo
    settings.github_pages_path = old_path
    settings.github_pages_dry_run = old_dry


@pytest.mark.asyncio
async def test_blog_full_chain_dry_run(blog_repo):
    pipe = TopicToBlogPipeline(generator=ContentGenerator(driver=FakeLLM()))
    req = BlogRequest(
        topic_name="我用 AI 把运维日报自动化了",
        keywords=["AI运维", "自动化", "降本增效"],
        persona={"tone": "技术博主"},
        categories=["AI运维"],
        description="AI 自动化运营实践",
    )
    res = await pipe.run(req)

    assert res.success is True
    assert res.title == "我用 AI 把运维日报自动化了"
    assert res.body_chars > 0
    assert res.tags == ["AI运维", "自动化", "降本增效"]
    # dry_run 预览里能看到 frontmatter 关键字段
    preview = res.raw["preview_first_500"]
    assert "title:" in preview
    assert "tags:" in preview
    assert "AI运维" in preview
    assert "description: AI 自动化运营实践" in preview
    # 中文 slug 已 percent-encode 进 URL
    assert "%" in (res.article_url or "")


@pytest.mark.asyncio
async def test_blog_publisher_failure_surfaced(blog_repo):
    """publisher 失败时，编排层如实回传 success=False + error。"""

    class FailingPublisher(GitHubPagesPublisher):
        async def publish(self, account_id, credential, content):
            return PublishResult(success=False, error="模拟构建失败")

    pipe = TopicToBlogPipeline(
        generator=ContentGenerator(driver=FakeLLM()),
        publisher=FailingPublisher(),
    )
    res = await pipe.run(BlogRequest(topic_name="x", keywords=["a"]))
    assert res.success is False
    assert res.error == "模拟构建失败"


@pytest.mark.asyncio
async def test_blog_title_fallback_to_topic(blog_repo):
    """生成结果没给 title 时，回退到主题名。"""

    class NoTitleLLM(LLMDriver):
        async def complete(self, system, user, **kwargs):
            return "正文内容，无标题"

    pipe = TopicToBlogPipeline(generator=ContentGenerator(driver=NoTitleLLM()))
    res = await pipe.run(BlogRequest(topic_name="兜底标题", keywords=["k"]))
    # generator 默认把 topic_name 作为 title（见 generator.generate 返回结构）
    assert res.title == "兜底标题"
    assert res.success is True
