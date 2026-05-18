"""ZhihuPublisher._check_published_url + _do_publish 单测 — 全 mock。

测试目标（publishing-sop §九 TODO 第 6 项闭环）：
  1. _check_published_url 纯函数验证：
     - /p/{id}/edit       → success=False（草稿）
     - /p/{id}            → success=True（公开）
     - /p/{id}/           → success=True（公开，容错末尾斜杠 + 归一化）
     - 其他形态 URL       → success=False（未知保守判失败）
     - 空 URL / None      → success=False
  2. _do_publish 接入验证：草稿 URL 不再被当成功返回（防虚假闭环）
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_ops.core.enums import ContentType, Platform, PublisherKind
from ai_ops.core.schemas import PublishContent, PublishResult
from ai_ops.publishers import zhihu as zh
from ai_ops.publishers.zhihu import (
    ZhihuPublisher,
    _check_published_url,
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _noop(*_a, **_kw):
        return None
    monkeypatch.setattr(zh.asyncio, "sleep", _noop)


# ============== _check_published_url 纯函数用例 ==============


def test_check_url_edit_suffix_is_draft():
    """/p/{id}/edit → 草稿，必须判 False，防止虚假闭环（系统说成功但实际未公开）。"""
    is_pub, norm = _check_published_url("https://zhuanlan.zhihu.com/p/12345/edit")
    assert is_pub is False
    assert norm == "https://zhuanlan.zhihu.com/p/12345/edit"  # 草稿态保留原 URL 供观测


def test_check_url_edit_suffix_with_trailing_slash_also_draft():
    """/p/{id}/edit/ 末尾斜杠也算草稿（容错）。"""
    is_pub, _ = _check_published_url("https://zhuanlan.zhihu.com/p/12345/edit/")
    assert is_pub is False


def test_check_url_bare_p_is_published():
    """/p/{id} 裸路径 = 真公开。"""
    is_pub, norm = _check_published_url("https://zhuanlan.zhihu.com/p/12345")
    assert is_pub is True
    assert norm == "https://zhuanlan.zhihu.com/p/12345"


def test_check_url_trailing_slash_is_published_and_normalized():
    """/p/{id}/ 末尾斜杠 = 真公开，且归一化去掉斜杠（避免下游 article_id 拿到空串）。"""
    is_pub, norm = _check_published_url("https://zhuanlan.zhihu.com/p/12345/")
    assert is_pub is True
    assert norm == "https://zhuanlan.zhihu.com/p/12345"
    # 验证归一化后 rsplit 拿得到正确 id
    assert norm.rsplit("/", 1)[-1] == "12345"


def test_check_url_unknown_pattern_is_failure():
    """非 /p/{digits} 形态 URL 一律保守判失败，不冒虚假闭环风险。"""
    # answer 类 URL（错跳到回答页）
    assert _check_published_url("https://www.zhihu.com/answer/789")[0] is False
    # question 类
    assert _check_published_url("https://www.zhihu.com/question/123")[0] is False
    # /p/ 但 id 非纯数字
    assert _check_published_url("https://zhuanlan.zhihu.com/p/abc123")[0] is False
    # 域名错（zhuanlan→www）
    assert _check_published_url("https://www.zhihu.com/p/12345")[0] is False
    # 完全无关 URL
    assert _check_published_url("https://example.com/anything")[0] is False


def test_check_url_empty_is_failure():
    """空 URL → 保守判失败。"""
    is_pub, norm = _check_published_url("")
    assert is_pub is False
    assert norm == ""


def test_check_url_http_scheme_supported():
    """http:// 也支持（虽然知乎全站 https，但正则允许两者，保险）。"""
    assert _check_published_url("http://zhuanlan.zhihu.com/p/12345")[0] is True


# ============== _do_publish 接入验证 ==============


def _content(title: str = "知乎测试标题", body: str = "正文 [link](http://x)",
             images: list[str] | None = None) -> PublishContent:
    return PublishContent(
        title=title,
        body=body,
        content_type=ContentType.IMAGE_TEXT,
        images=images or [],
    )


def _build_publish_page(final_url: str):
    """构造能跑完 _do_publish 全流程的 mock page，page.url 始终返回 final_url。"""
    page = MagicMock()
    page.goto = AsyncMock()
    page.click = AsyncMock()
    page.fill = AsyncMock()
    page.evaluate = AsyncMock()
    page.set_input_files = AsyncMock()
    page.wait_for_url = AsyncMock()

    # 发布按钮 mock
    btn = MagicMock()
    btn.scroll_into_view_if_needed = AsyncMock()
    btn.click = AsyncMock()
    page.wait_for_selector = AsyncMock(return_value=btn)

    type(page).url = property(lambda self: final_url)
    return page


def test_do_publish_returns_failure_when_url_is_draft():
    """final_url 是 /p/{id}/edit → _do_publish 必须返回 success=False，
    error 里说明草稿状态。这是本 task 核心：防虚假闭环。"""
    pub = ZhihuPublisher()
    draft_url = "https://zhuanlan.zhihu.com/p/12345/edit"
    page = _build_publish_page(draft_url)

    result: PublishResult = asyncio.run(pub._do_publish(page, _content()))

    assert result.success is False, "草稿 URL 不应被判 success——这是 publishing-sop §九 TODO 第 6 项核心修复"
    assert "草稿" in (result.error or "")
    assert result.platform_url == draft_url  # 草稿 URL 保留供运营人工排查
    assert result.raw_response["is_published"] is False


def test_do_publish_returns_success_with_public_url():
    """final_url 是裸 /p/{id} → success=True，且 platform_url 归一化、article_id 正确。"""
    pub = ZhihuPublisher()
    public_url = "https://zhuanlan.zhihu.com/p/98765"
    page = _build_publish_page(public_url)

    result = asyncio.run(pub._do_publish(page, _content()))

    assert result.success is True
    assert result.platform_url == public_url
    assert result.platform_post_id == "98765"
    assert result.raw_response["is_published"] is True


def test_do_publish_returns_success_with_trailing_slash_url():
    """final_url 是 /p/{id}/ 末尾斜杠 → success=True，平台 URL 归一化掉尾斜杠。"""
    pub = ZhihuPublisher()
    page = _build_publish_page("https://zhuanlan.zhihu.com/p/55555/")

    result = asyncio.run(pub._do_publish(page, _content()))

    assert result.success is True
    assert result.platform_url == "https://zhuanlan.zhihu.com/p/55555"
    assert result.platform_post_id == "55555"


def test_do_publish_returns_failure_when_url_pattern_unknown():
    """final_url 是其他形态（比如跳到了 zhihu /answer/）→ 保守判失败。"""
    pub = ZhihuPublisher()
    page = _build_publish_page("https://www.zhihu.com/answer/777")

    result = asyncio.run(pub._do_publish(page, _content()))

    assert result.success is False
    assert "草稿" in (result.error or "") or "URL 异常" in (result.error or "")


# ============== metadata 兜底 ==============


def test_metadata():
    pub = ZhihuPublisher()
    assert pub.platform == Platform.ZHIHU
    assert pub.kind == PublisherKind.SOCIAL_AUTO_UPLOAD
