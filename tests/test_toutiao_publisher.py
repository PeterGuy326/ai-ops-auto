"""ToutiaoPublisher 单测 — 全 mock，不依赖真账号/真浏览器。

测试目标：
  1. selector 常量存在性兜底
  2. _parse_count 数字解析契约（万/k/亿/纯数字/垃圾/None/空）
  3. _fetch_post_metadata 行为契约（成功返回 dict、降级返回 None）—
     上轮 P7-Z 的 _fetch_real_post_url 已重构为返回多字段 dict（Task C）
  4. _do_publish 末尾接入：platform_url 用 metadata.url，raw_response.initial_metadata 落盘
  5. collect_metrics 复用作品管理后台 navigate 路径采集互动数据
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ai_ops.core.enums import ContentType, Platform, PublisherKind
from ai_ops.core.schemas import PublishContent, PublishResult
from ai_ops.publishers import toutiao as tt
from ai_ops.publishers.toutiao import (
    ARTICLE_CARD_COMMENT_SELECTOR,
    ARTICLE_CARD_LIKE_SELECTOR,
    ARTICLE_CARD_VIEW_SELECTOR,
    PROFILE_ARTICLES_URL,
    SEL_ARTICLE_CARD,
    SEL_ARTICLE_CARD_ITEM_LINK,
    ToutiaoPublisher,
    _parse_count,
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
    # Task C 新增的 3 个互动指标 selector 必须存在且非空
    assert ARTICLE_CARD_VIEW_SELECTOR
    assert ARTICLE_CARD_COMMENT_SELECTOR
    assert ARTICLE_CARD_LIKE_SELECTOR


def test_metadata():
    pub = ToutiaoPublisher()
    assert pub.platform == Platform.TOUTIAO
    assert pub.kind == PublisherKind.SOCIAL_AUTO_UPLOAD


# ============== _parse_count 数字解析契约（Task C 新增）==============


def test_parse_count_supports_chinese_unit():
    """中文万单位：1.2万 → 12000，2万 → 20000，1.5亿 → 150000000。"""
    assert _parse_count("1.2万") == 12000
    assert _parse_count("2万") == 20000
    assert _parse_count("1.5亿") == 150000000


def test_parse_count_supports_k_unit():
    """k/K/w/W 单位：3.5k → 3500，10K → 10000，1.2w → 12000。"""
    assert _parse_count("3.5k") == 3500
    assert _parse_count("10K") == 10000
    assert _parse_count("1.2w") == 12000
    assert _parse_count("5W") == 50000


def test_parse_count_plain_numbers_and_whitespace():
    """纯数字 + 容忍前后空格：234 → 234，'  1000 ' → 1000。"""
    assert _parse_count("234") == 234
    assert _parse_count("  1000 ") == 1000
    assert _parse_count("0") == 0


def test_parse_count_handles_garbage():
    """垃圾输入统一降级为 0，不抛异常：'abc', '--', None, '' 全 0。"""
    assert _parse_count("abc") == 0
    assert _parse_count("--") == 0
    assert _parse_count(None) == 0
    assert _parse_count("") == 0
    assert _parse_count("   ") == 0


def test_parse_count_extracts_number_from_label_text():
    """text 邻接兜底场景：UI 文本可能是 '阅读 1234' 这种，从中抽数字。"""
    assert _parse_count("阅读 1234") == 1234
    assert _parse_count("评论 5") == 5


# ============== _fetch_post_metadata 行为用例（重构后契约）==============


def test_fetch_post_metadata_returns_all_fields_for_publish_path():
    """publish 路径（不传 match_post_id）：返回最新一张卡片的完整字段 dict。"""
    pub = ToutiaoPublisher()
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock(return_value={
        "url": "https://www.toutiao.com/item/7350001234567890123/",
        "view_count": "1.2万",
        "comment_count": "234",
        "like_count": "456",
        "share_count": "12",
        "publish_time": "刚刚",
    })

    result = asyncio.run(pub._fetch_post_metadata(page))

    assert result is not None
    assert result["url"] == "https://www.toutiao.com/item/7350001234567890123/"
    assert result["view_count"] == "1.2万"
    assert result["comment_count"] == "234"
    assert result["like_count"] == "456"
    page.goto.assert_awaited_once()
    args, _ = page.goto.call_args
    assert args[0] == PROFILE_ARTICLES_URL
    # publish 路径传空字典而不是 None（JS 那边 args.matchPostId 判断更稳）
    page.evaluate.assert_awaited_once()
    eval_args, _ = page.evaluate.call_args
    assert eval_args[1] == {}


def test_fetch_post_metadata_passes_match_post_id_for_collect_path():
    """collect 路径：传 match_post_id，JS 那边按 href 匹配卡片。"""
    pub = ToutiaoPublisher()
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock(return_value={
        "url": "https://www.toutiao.com/item/7350009999988887777/",
        "view_count": "888",
        "comment_count": "22",
        "like_count": "100",
        "share_count": "",
        "publish_time": "",
    })

    result = asyncio.run(pub._fetch_post_metadata(page, match_post_id="7350009999988887777"))

    assert result is not None
    assert "9999988887777" in result["url"]
    eval_args, _ = page.evaluate.call_args
    assert eval_args[1] == {"matchPostId": "7350009999988887777"}


def test_fetch_post_metadata_returns_none_on_no_card():
    """页面没渲染出 .article-card（结构变 / 文章未入库）→ 返回 None，不抛。"""
    pub = ToutiaoPublisher()
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock(side_effect=TimeoutError("article-card timeout"))
    page.evaluate = AsyncMock()  # 不应被调

    result = asyncio.run(pub._fetch_post_metadata(page))

    assert result is None
    page.evaluate.assert_not_awaited()


def test_fetch_post_metadata_returns_none_on_evaluate_none():
    """evaluate 返回 None（卡片整张没找到，例如 collect 路径 post_id 不匹配）→ 返回 None。"""
    pub = ToutiaoPublisher()
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock(return_value=None)

    result = asyncio.run(pub._fetch_post_metadata(page, match_post_id="not_exist"))

    assert result is None


def test_fetch_post_metadata_returns_none_on_goto_exception():
    """goto 抛网络异常 → 返回 None，不向上传播。

    publish 本身已成功，不能因为后置抓 metadata 失败而把整个 publish 拖垮。
    collect_metrics 调用方也需要这个保证——异常都拦在底层。"""
    pub = ToutiaoPublisher()
    page = MagicMock()
    page.goto = AsyncMock(side_effect=RuntimeError("network boom"))
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock()

    result = asyncio.run(pub._fetch_post_metadata(page))

    assert result is None


# ============== _do_publish 末尾接入验证 ==============


def _content(title: str = "头条测试标题", body: str = "正文 **加粗**",
             images: list[str] | None = None) -> PublishContent:
    return PublishContent(
        title=title,
        body=body,
        content_type=ContentType.IMAGE_TEXT,
        images=images or [],
    )


def _build_publish_page(*, metadata_dict: dict | None,
                        publish_url_before: str = "https://mp.toutiao.com/profile_v4/graphic/publish",
                        publish_url_after: str = "https://mp.toutiao.com/profile_v4/graphic/preview?xxx=1"):
    """构造一个能完整跑完 _do_publish 全流程的 mock page。

    metadata_dict: _fetch_post_metadata 内部 evaluate 的返回值——
      非 None 模拟抓到卡片字段；None 模拟卡片找不到（降级）"""
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.click = AsyncMock()
    page.fill = AsyncMock()
    page.evaluate = AsyncMock(return_value=metadata_dict)

    # 标题 / 发布按钮 / 确认按钮全部返回可点击 mock
    btn = MagicMock()
    btn.scroll_into_view_if_needed = AsyncMock()
    btn.click = AsyncMock()
    page.wait_for_selector = AsyncMock(return_value=btn)

    # url 是 property——发布前/发布后/抓 metadata 时连续值
    url_seq = [
        publish_url_before,  # final_btn 点击前
        publish_url_after,   # final_btn 点击后
        publish_url_after,   # _fetch_post_metadata 内部不再用 page.url（只用 evaluate）
    ]
    state = {"i": 0}
    def _get_url():
        v = url_seq[min(state["i"], len(url_seq) - 1)]
        state["i"] += 1
        return v
    type(page).url = property(lambda self: _get_url())

    return page


def test_do_publish_success_path_uses_real_url_from_metadata():
    """成功路径：_do_publish 返回的 platform_url 应该是 metadata.url（真链），
    raw_response.url_resolved_from_backend=True，raw_response.initial_metadata 落盘。

    这是 Task C 的关键闭环——publish 完成已经拿到第一份指标快照，
    worker 层可以直接落 Metrics 表，不用等 collect 飞轮 1h 后第一次跑。"""
    pub = ToutiaoPublisher()
    real = "https://www.toutiao.com/item/7350009999988887777/"
    meta = {
        "url": real,
        "view_count": "1.2万",
        "comment_count": "234",
        "like_count": "456",
        "share_count": "12",
        "publish_time": "刚刚",
    }
    page = _build_publish_page(metadata_dict=meta)

    result: PublishResult = asyncio.run(pub._do_publish(page, _content()))

    assert result.success is True
    assert result.platform_url == real
    assert result.raw_response["real_url"] == real
    assert result.raw_response["url_resolved_from_backend"] is True
    # final_url 仍保留发布页 URL 供观测
    assert "/item/" not in result.raw_response["final_url"]
    # initial_metadata 完整保存，下游 worker 可以落第一份 Metrics
    assert result.raw_response["initial_metadata"] == meta


def test_do_publish_success_falls_back_when_metadata_unavailable():
    """抓不到 metadata（卡片未渲染 / 异常）时 _do_publish 依然 success=True，
    platform_url 降级到发布页 URL，url_resolved_from_backend=False，
    initial_metadata 为空 dict（不是 None，避免下游 raw_response[''].get 炸）。"""
    pub = ToutiaoPublisher()
    page = _build_publish_page(metadata_dict=None)  # evaluate 返回 None → 整体 None

    result = asyncio.run(pub._do_publish(page, _content()))

    assert result.success is True
    assert result.platform_url == result.raw_response["final_url"]
    assert result.raw_response["url_resolved_from_backend"] is False
    assert result.raw_response["initial_metadata"] == {}


def test_do_publish_video_type_blocked_at_publish_entry():
    """video 类型应在 publish() 入口拦截。"""
    pub = ToutiaoPublisher()
    content = PublishContent(title="t", body="b", content_type=ContentType.VIDEO)
    result = asyncio.run(pub.publish(1, {"cookies": [{"name": "x", "value": "y"}]}, content))
    assert result.success is False
    assert "视频" in (result.error or "")


# ============== collect_metrics 行为用例（Task C 新增）==============


def test_collect_metrics_returns_zeros_when_credential_missing():
    """凭证缺 cookies → zeros + raw.error，不起浏览器（短路）。"""
    pub = ToutiaoPublisher()
    result = asyncio.run(pub.collect_metrics("7350001234567890123", None, {}))
    assert result == {"likes": 0, "comments": 0, "shares": 0, "views": 0,
                      "raw": {"error": "凭证缺 cookies"}}


def test_collect_metrics_returns_zeros_when_post_not_found(monkeypatch):
    """卡片找不到（_fetch_post_metadata 返回 None）→ zeros + raw.not_found=True。

    这是常见场景：文章已下架 / 被删 / 翻页超出第一页（暂未做翻页）—— 
    采集失败不等于异常，老老实实返回 zeros 让飞轮继续。"""
    pub = ToutiaoPublisher()

    # mock 掉整个 _fetch_post_metadata 直接返回 None
    async def _none(*_a, **_kw):
        return None
    monkeypatch.setattr(pub, "_fetch_post_metadata", _none)

    # mock playwright 启动链
    fake_browser = MagicMock()
    fake_browser.new_context = AsyncMock(return_value=_fake_context())
    fake_browser.close = AsyncMock()
    fake_p = MagicMock()
    fake_p.chromium.launch = AsyncMock(return_value=fake_browser)
    _patch_async_playwright(monkeypatch, fake_p)

    credential = {"cookies": [{"name": "tt_webid", "value": "abc", "domain": ".toutiao.com"}]}
    result = asyncio.run(pub.collect_metrics("7350001234567890123", None, credential))

    assert result["likes"] == 0
    assert result["comments"] == 0
    assert result["shares"] == 0
    assert result["views"] == 0
    assert result["raw"]["not_found"] is True
    assert result["raw"]["post_id"] == "7350001234567890123"


def test_collect_metrics_returns_real_counts_on_match(monkeypatch):
    """命中卡片 → 解析所有数字字段返回真数字，raw 里保留原始字符串供观测。

    这是 Task C 的核心 ROI 闭环——一次 navigate 抓全字段，
    不调头条创作中心数据接口（省签名 / 省风控）。"""
    pub = ToutiaoPublisher()

    async def _meta(*_a, **_kw):
        return {
            "url": "https://www.toutiao.com/item/7350009999988887777/",
            "view_count": "1.5万",
            "comment_count": "234",
            "like_count": "3.5k",
            "share_count": "12",
            "publish_time": "2小时前",
        }
    monkeypatch.setattr(pub, "_fetch_post_metadata", _meta)

    fake_browser = MagicMock()
    fake_browser.new_context = AsyncMock(return_value=_fake_context())
    fake_browser.close = AsyncMock()
    fake_p = MagicMock()
    fake_p.chromium.launch = AsyncMock(return_value=fake_browser)
    _patch_async_playwright(monkeypatch, fake_p)

    credential = {"cookies": [{"name": "tt_webid", "value": "abc", "domain": ".toutiao.com"}]}
    result = asyncio.run(pub.collect_metrics("7350009999988887777", None, credential))

    assert result["views"] == 15000   # 1.5万
    assert result["comments"] == 234
    assert result["likes"] == 3500    # 3.5k
    assert result["shares"] == 12
    assert result["raw"]["url"].endswith("/item/7350009999988887777/")
    # 原始字符串保留供观测
    assert result["raw"]["view_count_raw"] == "1.5万"
    assert result["raw"]["like_count_raw"] == "3.5k"


def test_collect_metrics_returns_zeros_on_playwright_exception(monkeypatch):
    """playwright 启动抛异常（容器没装 chromium / 反检测被拦）→
    zeros + raw.error，不向飞轮抛——飞轮调度方期望 dict 不期望异常。"""
    pub = ToutiaoPublisher()

    fake_p = MagicMock()
    fake_p.chromium.launch = AsyncMock(side_effect=RuntimeError("chromium not installed"))
    _patch_async_playwright(monkeypatch, fake_p)

    credential = {"cookies": [{"name": "tt_webid", "value": "abc"}]}
    result = asyncio.run(pub.collect_metrics("7350001234567890123", None, credential))

    assert result["likes"] == 0
    assert result["comments"] == 0
    assert result["shares"] == 0
    assert result["views"] == 0
    assert "error" in result["raw"]
    assert "chromium not installed" in result["raw"]["error"]


def _fake_context():
    ctx = MagicMock()
    ctx.add_cookies = AsyncMock()
    ctx.new_page = AsyncMock(return_value=MagicMock())
    return ctx


def _patch_async_playwright(monkeypatch, fake_p):
    """patch toutiao.get_async_playwright 让 `async with async_playwright() as p` 拿到 fake_p。"""
    class _Ctx:
        async def __aenter__(self_inner):
            return fake_p
        async def __aexit__(self_inner, *a):
            return None
    def _factory():
        return lambda: _Ctx()
    monkeypatch.setattr(tt, "get_async_playwright", _factory)
