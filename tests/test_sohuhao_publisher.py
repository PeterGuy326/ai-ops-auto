"""SohuhaoPublisher 单测 — 全 mock，不依赖真账号/真浏览器。

测试目标（与 test_toutiao_publisher.py / test_zhihu_publisher.py 套路对称）：
  1. Platform.SOHUHAO 枚举存在 + publisher 注册
  2. _check_published_url 纯函数防虚假闭环
  3. _do_publish 成功 / 草稿 URL 拦截
  4. _fetch_post_metadata 行为契约（成功 dict / 异常 None）
  5. collect_metrics 复用 fetch 路径 + 失败降级
  6. 防 substring 反面教训：发布按钮严格用 :text-is
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_ops.core.enums import ContentType, Platform, PublisherKind
from ai_ops.core.schemas import PublishContent, PublishResult
from ai_ops.publishers import sohuhao as shh
from ai_ops.publishers.registry import default_registry
from ai_ops.publishers.sohuhao import (
    ARTICLE_CARD_COMMENT_SELECTOR,
    ARTICLE_CARD_LIKE_SELECTOR,
    ARTICLE_CARD_VIEW_SELECTOR,
    CONTENT_EDITOR_SELECTOR,
    COVER_UPLOAD_INPUT_SELECTOR,
    EDITOR_URL,
    PROFILE_ARTICLES_URL,
    PUBLISH_BUTTON_SELECTOR,
    SEL_ARTICLE_CARD,
    SohuhaoPublisher,
    TITLE_INPUT_SELECTOR,
    _check_published_url,
)


# ============== 全局：mock 掉 random 延迟，让单测秒过 ==============


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _noop(*_a, **_kw):
        return None
    monkeypatch.setattr(shh.asyncio, "sleep", _noop)


# ============== Platform 枚举 + 注册（Round 2B 准入门槛）==============


def test_platform_enum_added():
    """Platform.SOHUHAO 必须存在且 value=sohuhao（小写下划线约定）。"""
    assert hasattr(Platform, "SOHUHAO")
    assert Platform.SOHUHAO.value == "sohuhao"


def test_publisher_registered():
    """registry.resolve(SOHUHAO) 必须返回至少一个 SohuhaoPublisher 实例。"""
    pubs = default_registry.resolve(Platform.SOHUHAO)
    assert pubs, "SohuhaoPublisher 未注册到 default_registry"
    assert any(isinstance(p, SohuhaoPublisher) for p in pubs)


def test_metadata():
    pub = SohuhaoPublisher()
    assert pub.platform == Platform.SOHUHAO
    assert pub.kind == PublisherKind.SOCIAL_AUTO_UPLOAD


def test_selectors_defined():
    """顶部常量集中，新增 selector 必须非空（防散落函数体内）。"""
    assert EDITOR_URL.startswith("https://")
    assert PROFILE_ARTICLES_URL.startswith("https://")
    assert TITLE_INPUT_SELECTOR
    assert CONTENT_EDITOR_SELECTOR
    assert COVER_UPLOAD_INPUT_SELECTOR
    assert PUBLISH_BUTTON_SELECTOR
    assert SEL_ARTICLE_CARD
    assert ARTICLE_CARD_VIEW_SELECTOR
    assert ARTICLE_CARD_COMMENT_SELECTOR
    assert ARTICLE_CARD_LIKE_SELECTOR


# ============== 防 substring 反面教训（publishing-sop §三-B 核心）==============


def test_publish_button_uses_text_is_not_substring():
    """发布按钮必须严格 :text-is 不可用 has-text substring。

    publishing-sop §三-B 反面教训：substring 匹配第一个含字的元素会点错
    （命中「发布设置 / 发布历史 / 取消发布」），导致整次发布被埋。
    本用例 hard-fail 任何 has-text 使用，作为编译期防火墙。"""
    assert ":text-is(" in PUBLISH_BUTTON_SELECTOR
    assert "has-text(" not in PUBLISH_BUTTON_SELECTOR, (
        "禁用 has-text substring，必须用 :text-is 精确匹配（详见 publishing-sop §三-B）"
    )


# ============== _check_published_url 纯函数用例（防虚假闭环）==============


def test_check_url_normal_format_is_published():
    """搜狐公开 URL https://www.sohu.com/a/<id>_<author> → success=True 且归一化。"""
    is_pub, norm = _check_published_url("https://www.sohu.com/a/812345678_999888")
    assert is_pub is True
    assert norm == "https://www.sohu.com/a/812345678_999888"


def test_check_url_trailing_slash_is_published_and_normalized():
    """末尾斜杠 → 公开，归一化去掉斜杠（便于下游 article_id 提取）。"""
    is_pub, norm = _check_published_url("https://www.sohu.com/a/812345678_999888/")
    assert is_pub is True
    assert norm == "https://www.sohu.com/a/812345678_999888"
    # 提取 article_id 校验
    assert norm.rsplit("/", 1)[-1] == "812345678_999888"


def test_check_url_query_param_is_published_and_stripped():
    """带查询参数 → 公开，归一化去掉 query（spm/from 等 tracking 不进 platform_url）。"""
    is_pub, norm = _check_published_url("https://www.sohu.com/a/812345678_999888?spm=trk")
    assert is_pub is True
    assert norm == "https://www.sohu.com/a/812345678_999888"


def test_check_url_edit_suffix_is_draft():
    """/edit 后缀 → 草稿，无条件 False（防虚假闭环：系统说成功但实际未公开）。"""
    is_pub, norm = _check_published_url("https://mp.sohu.com/article/edit/123456")
    assert is_pub is False
    # 草稿态保留原 URL 供运营人工排查
    assert "edit" in norm


def test_check_url_draft_suffix_is_failure():
    """/draft 后缀 → 草稿，与 /edit 同语义判 False。"""
    is_pub, _ = _check_published_url("https://mp.sohu.com/article/draft/123")
    assert is_pub is False


def test_check_url_empty_is_failure():
    """空 URL → 保守判失败，不冒虚假闭环风险。"""
    is_pub, norm = _check_published_url("")
    assert is_pub is False
    assert norm == ""


def test_check_url_unknown_pattern_is_failure():
    """非 /a/<id>_<author> 形态 URL 一律保守判失败。"""
    # 错跳到 mp 后台首页
    assert _check_published_url("https://mp.sohu.com/mpfe/v3/main/home")[0] is False
    # /a/ 但 id 缺 author 部分（业界另一种形态，本 publisher 不接受）
    assert _check_published_url("https://www.sohu.com/a/812345678")[0] is False
    # 完全无关 URL
    assert _check_published_url("https://example.com/anything")[0] is False
    # 域名错（sohu.com 但走 sub 子域名）
    assert _check_published_url("https://news.sohu.com/a/123_456")[0] is False


# ============== _do_publish 行为用例 ==============


def _content(title: str = "搜狐号测试标题", body: str = "正文 **加粗** [link](http://x)",
             images: list[str] | None = None) -> PublishContent:
    return PublishContent(
        title=title,
        body=body,
        content_type=ContentType.IMAGE_TEXT,
        images=images or [],
    )


def _build_publish_page(*, metadata_dict: dict | None,
                        url_before: str = "https://mp.sohu.com/mpfe/v3/main/editor",
                        url_after: str = "https://mp.sohu.com/mpfe/v3/main/editor?published=1"):
    """构造能完整跑完 _do_publish 全流程的 mock page。

    metadata_dict: _fetch_post_metadata 内部 evaluate 的返回值 —
      非 None 模拟抓到卡片字段；None 模拟卡片找不到（降级）。
    """
    page = MagicMock()
    page.goto = AsyncMock()
    page.click = AsyncMock()
    page.fill = AsyncMock()
    page.set_input_files = AsyncMock()
    page.evaluate = AsyncMock(return_value=metadata_dict)

    btn = MagicMock()
    btn.scroll_into_view_if_needed = AsyncMock()
    btn.click = AsyncMock()
    page.wait_for_selector = AsyncMock(return_value=btn)

    url_seq = [url_before, url_after, url_after]
    state = {"i": 0}
    def _get_url():
        v = url_seq[min(state["i"], len(url_seq) - 1)]
        state["i"] += 1
        return v
    type(page).url = property(lambda self: _get_url())
    return page


def test_do_publish_success_returns_url():
    """成功路径：metadata.url 是搜狐公开链 → success=True、platform_url 用真链。"""
    pub = SohuhaoPublisher()
    real = "https://www.sohu.com/a/812345678_999888"
    meta = {
        "url": real,
        "view_count": "1.2万",
        "comment_count": "234",
        "like_count": "56",
        "share_count": "12",
        "publish_time": "刚刚",
    }
    page = _build_publish_page(metadata_dict=meta)

    result: PublishResult = asyncio.run(pub._do_publish(page, _content()))

    assert result.success is True
    assert result.platform_url == real
    assert result.platform_post_id == "812345678_999888"
    assert result.raw_response["real_url"] == real
    assert result.raw_response["url_resolved_from_backend"] is True
    assert result.raw_response["is_published"] is True
    assert result.raw_response["initial_metadata"] == meta


def test_do_publish_returns_failure_when_url_is_draft():
    """metadata.url 含 /edit → _check_published_url 判 False → _do_publish 必须 success=False。

    防虚假闭环（publishing-sop §三-B 核心）：草稿 URL 不能被当 SUCCESS 返回。"""
    pub = SohuhaoPublisher()
    draft = "https://mp.sohu.com/article/edit/123456"
    meta = {"url": draft, "view_count": "", "comment_count": "", "like_count": "",
            "share_count": "", "publish_time": ""}
    page = _build_publish_page(metadata_dict=meta)

    result = asyncio.run(pub._do_publish(page, _content()))

    assert result.success is False, "草稿 URL 不应被判 success——防虚假闭环"
    assert "草稿" in (result.error or "") or "URL 异常" in (result.error or "")
    assert result.platform_url == draft  # 草稿 URL 保留供运营人工排查
    assert result.raw_response["is_published"] is False
    # initial_metadata 仍然落盘供观测
    assert result.raw_response["initial_metadata"] == meta


def test_do_publish_video_type_blocked_at_publish_entry():
    """video 类型应在 publish() 入口拦截，不进 _do_publish。"""
    pub = SohuhaoPublisher()
    content = PublishContent(title="t", body="b", content_type=ContentType.VIDEO)
    result = asyncio.run(pub.publish(1, {"cookies": [{"name": "x", "value": "y"}]}, content))
    assert result.success is False
    assert "视频" in (result.error or "")


def test_publish_returns_failure_when_credential_missing_cookies():
    """凭证缺 cookies → publish 入口短路返回 success=False，不起浏览器。"""
    pub = SohuhaoPublisher()
    content = _content()
    result = asyncio.run(pub.publish(1, {}, content))
    assert result.success is False
    assert "cookies" in (result.error or "")


# ============== _fetch_post_metadata 行为契约 ==============


def test_fetch_post_metadata_returns_all_fields_for_publish_path():
    """publish 路径（match_post_id=None）：evaluate 拿 {} 参数 + goto PROFILE。"""
    pub = SohuhaoPublisher()
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock(return_value={
        "url": "https://www.sohu.com/a/812345678_999888",
        "view_count": "1.5万",
        "comment_count": "88",
        "like_count": "123",
        "share_count": "5",
        "publish_time": "1小时前",
    })

    result = asyncio.run(pub._fetch_post_metadata(page))

    assert result is not None
    assert result["url"].endswith("_999888")
    assert result["view_count"] == "1.5万"
    page.goto.assert_awaited_once()
    args, _ = page.goto.call_args
    assert args[0] == PROFILE_ARTICLES_URL
    # publish 路径：evaluate 第二参为空 dict
    eval_args, _ = page.evaluate.call_args
    assert eval_args[1] == {}


def test_fetch_post_metadata_returns_zeros_when_card_not_found():
    """evaluate 返回 None（卡片整张未找到）→ _fetch_post_metadata 返回 None，不抛。"""
    pub = SohuhaoPublisher()
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)

    result = asyncio.run(pub._fetch_post_metadata(page, match_post_id="not_exist"))

    assert result is None


def test_fetch_post_metadata_returns_none_on_exception():
    """goto/wait 任意异常 → 返回 None，**不向上传播**（保证 publish/collect 调用方降级）。"""
    pub = SohuhaoPublisher()
    page = MagicMock()
    page.goto = AsyncMock(side_effect=RuntimeError("network boom"))
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock()

    result = asyncio.run(pub._fetch_post_metadata(page))

    assert result is None


# ============== collect_metrics 行为用例 ==============


def _fake_context():
    ctx = MagicMock()
    ctx.add_cookies = AsyncMock()
    ctx.new_page = AsyncMock(return_value=MagicMock())
    return ctx


def _patch_async_playwright(monkeypatch, fake_p):
    """patch sohuhao.get_async_playwright 让 `async with async_playwright() as p` 拿到 fake_p。"""
    class _Ctx:
        async def __aenter__(self_inner):
            return fake_p
        async def __aexit__(self_inner, *a):
            return None
    def _factory():
        return lambda: _Ctx()
    monkeypatch.setattr(shh, "get_async_playwright", _factory)


def test_collect_metrics_returns_zeros_when_credential_missing():
    """凭证缺 cookies → zeros + raw.error，不起浏览器（短路）。"""
    pub = SohuhaoPublisher()
    result = asyncio.run(pub.collect_metrics("812345678_999888", None, {}))
    assert result == {"likes": 0, "comments": 0, "shares": 0, "views": 0,
                      "raw": {"error": "凭证缺 cookies"}}


def test_collect_metrics_returns_real_counts_on_match(monkeypatch):
    """命中卡片 → 解析所有数字字段返回真数字，raw 保留原始字符串供观测。

    核心 ROI 闭环：一次 navigate 抓全字段，不调搜狐第三方数据接口。"""
    pub = SohuhaoPublisher()

    async def _meta(*_a, **_kw):
        return {
            "url": "https://www.sohu.com/a/812345678_999888",
            "view_count": "2.5万",
            "comment_count": "456",
            "like_count": "1.2k",
            "share_count": "33",
            "publish_time": "3小时前",
        }
    monkeypatch.setattr(pub, "_fetch_post_metadata", _meta)

    fake_browser = MagicMock()
    fake_browser.new_context = AsyncMock(return_value=_fake_context())
    fake_browser.close = AsyncMock()
    fake_p = MagicMock()
    fake_p.chromium.launch = AsyncMock(return_value=fake_browser)
    _patch_async_playwright(monkeypatch, fake_p)

    credential = {"cookies": [{"name": "passport", "value": "abc", "domain": ".sohu.com"}]}
    result = asyncio.run(pub.collect_metrics("812345678_999888", None, credential))

    assert result["views"] == 25000   # 2.5万
    assert result["comments"] == 456
    assert result["likes"] == 1200    # 1.2k
    assert result["shares"] == 33
    assert result["raw"]["url"].endswith("_999888")
    assert result["raw"]["view_count_raw"] == "2.5万"
    assert result["raw"]["like_count_raw"] == "1.2k"


def test_collect_metrics_returns_zeros_when_post_not_found(monkeypatch):
    """卡片找不到（_fetch_post_metadata 返回 None）→ zeros + raw.not_found=True。"""
    pub = SohuhaoPublisher()

    async def _none(*_a, **_kw):
        return None
    monkeypatch.setattr(pub, "_fetch_post_metadata", _none)

    fake_browser = MagicMock()
    fake_browser.new_context = AsyncMock(return_value=_fake_context())
    fake_browser.close = AsyncMock()
    fake_p = MagicMock()
    fake_p.chromium.launch = AsyncMock(return_value=fake_browser)
    _patch_async_playwright(monkeypatch, fake_p)

    credential = {"cookies": [{"name": "passport", "value": "abc"}]}
    result = asyncio.run(pub.collect_metrics("812345678_999888", None, credential))

    assert result["likes"] == 0
    assert result["comments"] == 0
    assert result["shares"] == 0
    assert result["views"] == 0
    assert result["raw"]["not_found"] is True
    assert result["raw"]["post_id"] == "812345678_999888"


def test_collect_metrics_returns_zeros_on_playwright_exception(monkeypatch):
    """playwright 启动抛异常 → zeros + raw.error，不向飞轮抛。"""
    pub = SohuhaoPublisher()

    fake_p = MagicMock()
    fake_p.chromium.launch = AsyncMock(side_effect=RuntimeError("chromium not installed"))
    _patch_async_playwright(monkeypatch, fake_p)

    credential = {"cookies": [{"name": "passport", "value": "abc"}]}
    result = asyncio.run(pub.collect_metrics("812345678_999888", None, credential))

    assert result["likes"] == 0
    assert result["comments"] == 0
    assert "error" in result["raw"]
    assert "chromium not installed" in result["raw"]["error"]
