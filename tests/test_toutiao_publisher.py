"""ToutiaoPublisher._fetch_real_post_url 单测 — 全 mock，不依赖真账号/真浏览器。

测试目标（publishing-sop §九 TODO 第 5 项闭环）：
  1. 真链抓取成功路径：作品管理后台拉到 /item/{id}/，覆盖发布页 URL
  2. 抓不到时降级到原 URL（不抛、不破坏发布闭环）
  3. _do_publish 末尾正确接入抓真链流程，platform_url 是真链不是发布页 URL
  4. selector 常量存在性兜底
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_ops.core.enums import ContentType, Platform, PublisherKind
from ai_ops.core.schemas import PublishContent, PublishResult
from ai_ops.publishers import toutiao as tt
from ai_ops.publishers.toutiao import (
    PROFILE_ARTICLES_URL,
    SEL_ARTICLE_CARD,
    SEL_ARTICLE_CARD_ITEM_LINK,
    ToutiaoPublisher,
)


# ============== 全局：mock 掉 random 延迟，让单测秒过 ==============


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _noop(*_a, **_kw):
        return None
    monkeypatch.setattr(tt.asyncio, "sleep", _noop)


# ============== selector 常量兜底 ==============


def test_selectors_defined():
    """新增 selector 常量必须集中在顶部、非空，避免散落在函数体内。"""
    assert PROFILE_ARTICLES_URL == "https://mp.toutiao.com/profile_v4/graphic/articles"
    assert SEL_ARTICLE_CARD == ".article-card"
    assert "/item/" in SEL_ARTICLE_CARD_ITEM_LINK


def test_metadata():
    pub = ToutiaoPublisher()
    assert pub.platform == Platform.TOUTIAO
    assert pub.kind == PublisherKind.SOCIAL_AUTO_UPLOAD


# ============== _fetch_real_post_url 行为用例 ==============


def test_fetch_real_post_url_success():
    """真链抓取成功：作品管理后台返回 /item/{id}/，覆盖原发布页 URL。"""
    pub = ToutiaoPublisher()
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock(return_value="https://www.toutiao.com/item/7350001234567890123/")

    fallback = "https://mp.toutiao.com/profile_v4/graphic/publish?somestate=1"
    result = asyncio.run(pub._fetch_real_post_url(page, fallback))

    assert result == "https://www.toutiao.com/item/7350001234567890123/"
    # 必须真的 navigate 到作品管理后台
    page.goto.assert_awaited_once()
    args, kwargs = page.goto.call_args
    assert args[0] == PROFILE_ARTICLES_URL
    page.wait_for_selector.assert_awaited_once()
    page.evaluate.assert_awaited_once()


def test_fetch_real_post_url_fallback_on_no_card():
    """页面没渲染出 .article-card（结构变 / 文章未入库）→ 降级到 fallback，不抛。"""
    pub = ToutiaoPublisher()
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock(side_effect=TimeoutError("article-card timeout"))
    page.evaluate = AsyncMock()  # 不应被调

    fallback = "https://mp.toutiao.com/profile_v4/graphic/publish?finalpage=1"
    result = asyncio.run(pub._fetch_real_post_url(page, fallback))

    assert result == fallback  # 降级
    page.evaluate.assert_not_awaited()  # 卡片都没出来不应再 evaluate


def test_fetch_real_post_url_fallback_on_evaluate_none():
    """evaluate 返回 None（卡片在但没 a[href*=/item/]）→ 降级到 fallback。"""
    pub = ToutiaoPublisher()
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)

    fallback = "https://mp.toutiao.com/profile_v4/somewhere"
    result = asyncio.run(pub._fetch_real_post_url(page, fallback))

    assert result == fallback


def test_fetch_real_post_url_fallback_on_goto_exception():
    """goto 抛网络异常 → 降级到 fallback，不向上传播。
    
    publish 本身已成功，不能因为后置抓真链失败而把整个 publish 拖垮。"""
    pub = ToutiaoPublisher()
    page = MagicMock()
    page.goto = AsyncMock(side_effect=RuntimeError("network boom"))
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock()

    fallback = "https://mp.toutiao.com/preview"
    result = asyncio.run(pub._fetch_real_post_url(page, fallback))

    assert result == fallback


# ============== _do_publish 末尾接入验证 ==============


def _content(title: str = "头条测试标题", body: str = "正文 **加粗**",
             images: list[str] | None = None) -> PublishContent:
    return PublishContent(
        title=title,
        body=body,
        content_type=ContentType.IMAGE_TEXT,
        images=images or [],
    )


def _build_publish_page(*, real_url: str | None,
                        publish_url_before: str = "https://mp.toutiao.com/profile_v4/graphic/publish",
                        publish_url_after: str = "https://mp.toutiao.com/profile_v4/graphic/preview?xxx=1"):
    """构造一个能完整跑完 _do_publish 全流程的 mock page。"""
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.click = AsyncMock()
    page.fill = AsyncMock()
    page.evaluate = AsyncMock(return_value=real_url)

    # 标题 / 发布按钮 / 确认按钮全部返回可点击 mock
    btn = MagicMock()
    btn.scroll_into_view_if_needed = AsyncMock()
    btn.click = AsyncMock()
    page.wait_for_selector = AsyncMock(return_value=btn)

    # url 是 property 一样的属性——发布前/发布后/抓真链时的连续值
    # 用 PropertyMock 太重，直接预填一个状态机
    url_seq = [
        publish_url_before,  # final_btn 点击前
        publish_url_after,   # final_btn 点击后
        publish_url_after,   # _fetch_real_post_url 内部不再用 page.url（只用 evaluate）
    ]
    state = {"i": 0}
    def _get_url():
        v = url_seq[min(state["i"], len(url_seq) - 1)]
        state["i"] += 1
        return v
    type(page).url = property(lambda self: _get_url())

    return page


def test_do_publish_success_path_uses_real_url():
    """成功路径：_do_publish 返回的 platform_url 应该是真链不是发布页 URL，
    且 raw_response.url_resolved_from_backend=True 标记真链确实被抓到。"""
    pub = ToutiaoPublisher()
    real = "https://www.toutiao.com/item/7350009999988887777/"
    page = _build_publish_page(real_url=real)

    result: PublishResult = asyncio.run(pub._do_publish(page, _content()))

    assert result.success is True
    assert result.platform_url == real
    assert result.raw_response["real_url"] == real
    assert result.raw_response["url_resolved_from_backend"] is True
    # final_url 仍保留发布页 URL 供观测
    assert "/item/" not in result.raw_response["final_url"]


def test_do_publish_success_falls_back_when_real_url_unavailable():
    """抓不到真链时 _do_publish 依然 success=True，platform_url 降级到发布页 URL，
    raw_response.url_resolved_from_backend=False 让上层知道这是降级值。"""
    pub = ToutiaoPublisher()
    page = _build_publish_page(real_url=None)  # evaluate 返回 None → 降级

    result = asyncio.run(pub._do_publish(page, _content()))

    assert result.success is True
    assert result.platform_url == result.raw_response["final_url"]
    assert result.raw_response["url_resolved_from_backend"] is False


def test_do_publish_video_type_blocked_at_publish_entry():
    """video 类型应在 publish() 入口拦截。"""
    pub = ToutiaoPublisher()
    content = PublishContent(title="t", body="b", content_type=ContentType.VIDEO)
    result = asyncio.run(pub.publish(1, {"cookies": [{"name": "x", "value": "y"}]}, content))
    assert result.success is False
    assert "视频" in (result.error or "")
