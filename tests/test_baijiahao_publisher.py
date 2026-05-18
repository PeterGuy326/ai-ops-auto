"""BaijiahaoPublisher 单测 — 全 mock，不依赖真账号/真浏览器。

测试目标：
  1. Platform.BAIJIAHAO 枚举 + registry 注册兜底
  2. _check_published_url 纯函数：公开/草稿/未知/空 四种边界
  3. _do_publish 行为契约：成功路径返真链 / 草稿 URL 防虚假闭环
  4. _fetch_post_metadata：找不到卡片降级 None / 异常降级 None
  5. collect_metrics：cookies 缺失短路 / 命中返真数字 / 卡片不存在返 zeros
  6. **bonus**：防 substring 反面教训 —— grep 确认发布按钮用 :text-is 不用 has-text

测试套路套 test_toutiao_publisher.py + test_zhihu_publisher.py。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_ops.core.enums import ContentType, Platform, PublisherKind
from ai_ops.core.schemas import PublishContent, PublishResult
from ai_ops.publishers import baijiahao as bjh
from ai_ops.publishers.baijiahao import (
    ARTICLE_CARD_COMMENT_SELECTOR,
    ARTICLE_CARD_LIKE_SELECTOR,
    ARTICLE_CARD_VIEW_SELECTOR,
    BaijiahaoPublisher,
    CONTENT_EDITOR_SELECTOR,
    COVER_UPLOAD_INPUT_SELECTOR,
    EDITOR_URL,
    PROFILE_ARTICLES_URL,
    PUBLISH_BUTTON_SELECTOR,
    SEL_ARTICLE_CARD,
    TITLE_INPUT_SELECTOR,
    _check_published_url,
)


# ============== 全局：mock 掉 random 延迟，让单测秒过 ==============


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _noop(*_a, **_kw):
        return None
    monkeypatch.setattr(bjh.asyncio, "sleep", _noop)


# ============== Platform 枚举 + 注册 ==============


def test_platform_enum_added():
    """Platform.BAIJIAHAO 必须存在（DONE-1）。"""
    assert hasattr(Platform, "BAIJIAHAO")
    assert Platform.BAIJIAHAO.value == "baijiahao"


def test_publisher_registered():
    """default_registry.resolve(BAIJIAHAO) 必须返回 BaijiahaoPublisher 实例（DONE-2）。"""
    from ai_ops.publishers.registry import default_registry
    pubs = default_registry.resolve(Platform.BAIJIAHAO)
    assert pubs, "BAIJIAHAO 平台未注册任何 publisher"
    assert any(isinstance(p, BaijiahaoPublisher) for p in pubs)


def test_metadata():
    pub = BaijiahaoPublisher()
    assert pub.platform == Platform.BAIJIAHAO
    assert pub.kind == PublisherKind.SOCIAL_AUTO_UPLOAD


# ============== selector 常量兜底 ==============


def test_selectors_defined():
    """selector + URL 常量必须集中在顶部、非空 —— 防散落在函数体内。"""
    assert EDITOR_URL.startswith("https://baijiahao.baidu.com")
    assert PROFILE_ARTICLES_URL == "https://baijiahao.baidu.com/builder/rc/content"
    assert TITLE_INPUT_SELECTOR
    assert CONTENT_EDITOR_SELECTOR
    assert COVER_UPLOAD_INPUT_SELECTOR
    assert PUBLISH_BUTTON_SELECTOR
    assert SEL_ARTICLE_CARD
    assert ARTICLE_CARD_VIEW_SELECTOR
    assert ARTICLE_CARD_COMMENT_SELECTOR
    assert ARTICLE_CARD_LIKE_SELECTOR


# ============== _check_published_url 纯函数用例 ==============


def test_check_url_normal_format_is_published():
    """baijiahao.baidu.com/s?id=<digits> = 公开。"""
    is_pub, norm = _check_published_url("https://baijiahao.baidu.com/s?id=1234567890")
    assert is_pub is True
    assert norm == "https://baijiahao.baidu.com/s?id=1234567890"


def test_check_url_with_extra_query_is_published():
    """带额外 query 参数（&from=...）的公开 URL 也算公开。"""
    url = "https://baijiahao.baidu.com/s?id=1234567890&wfr=spider&for=pc"
    is_pub, _ = _check_published_url(url)
    assert is_pub is True


def test_check_url_edit_suffix_is_draft():
    """含 /builder/rc/edit → 草稿，必须判 False（防虚假闭环：发布按钮未真发时停在编辑器）。"""
    is_pub, norm = _check_published_url(
        "https://baijiahao.baidu.com/builder/rc/edit?id=abc"
    )
    assert is_pub is False
    assert norm == "https://baijiahao.baidu.com/builder/rc/edit?id=abc"


def test_check_url_generic_edit_suffix_also_draft():
    """通用 /edit 末尾兜底也算草稿。"""
    is_pub, _ = _check_published_url("https://baijiahao.baidu.com/foo/edit")
    assert is_pub is False
    is_pub2, _ = _check_published_url("https://baijiahao.baidu.com/foo/edit/")
    assert is_pub2 is False


def test_check_url_empty_is_failure():
    """空 URL / None → 保守判失败，归一化返回空串。"""
    is_pub, norm = _check_published_url("")
    assert is_pub is False
    assert norm == ""
    is_pub2, norm2 = _check_published_url(None)  # type: ignore[arg-type]
    assert is_pub2 is False


def test_check_url_unknown_pattern_is_failure():
    """非 /s?id=<digits> 形态 URL 一律保守判失败 —— 不冒虚假闭环风险。"""
    # 错域名
    assert _check_published_url("https://www.baidu.com/s?id=12345")[0] is False
    # /s 但 id 非纯数字
    assert _check_published_url("https://baijiahao.baidu.com/s?id=abc")[0] is False
    # 错路径
    assert _check_published_url("https://baijiahao.baidu.com/article/12345")[0] is False
    # 完全无关 URL
    assert _check_published_url("https://example.com/anything")[0] is False
    # 头条 URL（防误判跨域）
    assert _check_published_url("https://www.toutiao.com/item/7350001234567890123/")[0] is False


def test_check_url_http_scheme_supported():
    """http:// 也支持（虽然百度全站 https，但正则允许两者）。"""
    assert _check_published_url("http://baijiahao.baidu.com/s?id=12345")[0] is True


# ============== _do_publish 行为契约 ==============


def _content(title: str = "百家号测试标题", body: str = "正文 **加粗**",
             images: list[str] | None = None) -> PublishContent:
    return PublishContent(
        title=title,
        body=body,
        content_type=ContentType.IMAGE_TEXT,
        images=images or [],
    )


def _build_publish_page(*, metadata_dict: dict | None,
                        fallback_page_url: str = "https://baijiahao.baidu.com/builder/rc/edit?id=draft123"):
    """构造一个能完整跑完 _do_publish 全流程的 mock page。

    metadata_dict: _fetch_post_metadata 内部 evaluate 的返回值。
    fallback_page_url: 抓不到 metadata 时降级用的 page.url（默认草稿态）"""
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.click = AsyncMock()
    page.fill = AsyncMock()
    page.set_input_files = AsyncMock()
    page.evaluate = AsyncMock(return_value=metadata_dict)

    btn = MagicMock()
    btn.scroll_into_view_if_needed = AsyncMock()
    btn.click = AsyncMock()
    page.wait_for_selector = AsyncMock(return_value=btn)

    type(page).url = property(lambda self: fallback_page_url)
    return page


def test_do_publish_success_returns_url():
    """成功路径：metadata 抓到公开 URL → success=True，platform_url 是公开 URL，
    post_id 从 /s?id=<digits> 解析出来。"""
    pub = BaijiahaoPublisher()
    real_url = "https://baijiahao.baidu.com/s?id=1730009999988887777"
    meta = {
        "url": real_url,
        "view_count": "1.2万",
        "comment_count": "234",
        "like_count": "456",
        "share_count": "12",
        "publish_time": "刚刚",
    }
    page = _build_publish_page(metadata_dict=meta)

    result: PublishResult = asyncio.run(pub._do_publish(page, _content()))

    assert result.success is True
    assert result.platform_url == real_url
    assert result.platform_post_id == "1730009999988887777"
    assert result.raw_response["real_url"] == real_url
    assert result.raw_response["url_resolved_from_backend"] is True
    assert result.raw_response["is_published"] is True
    # Task C 闭环：第一份 metadata 快照落 raw_response
    assert result.raw_response["initial_metadata"] == meta


def test_do_publish_returns_failure_when_url_is_draft():
    """抓不到 metadata 时降级到 page.url（草稿编辑器 URL）→ _check_published_url
    判 False → success=False，error 含「草稿」，防虚假闭环。"""
    pub = BaijiahaoPublisher()
    # metadata=None 触发降级到 page.url（默认 /builder/rc/edit?id=draft123）
    page = _build_publish_page(metadata_dict=None)

    result = asyncio.run(pub._do_publish(page, _content()))

    assert result.success is False, "草稿 URL 不应被判 success——防虚假闭环"
    assert "草稿" in (result.error or "")
    assert result.platform_url and "/builder/rc/edit" in result.platform_url
    assert result.raw_response["is_published"] is False
    assert result.raw_response["url_resolved_from_backend"] is False


def test_do_publish_video_type_blocked_at_publish_entry():
    """video 类型应在 publish() 入口拦截。"""
    pub = BaijiahaoPublisher()
    content = PublishContent(title="t", body="b", content_type=ContentType.VIDEO)
    result = asyncio.run(pub.publish(1, {"cookies": [{"name": "x", "value": "y"}]}, content))
    assert result.success is False
    assert "视频" in (result.error or "")


def test_publish_returns_failure_when_credential_missing():
    """凭证缺 cookies → 短路返回 failure，不起浏览器。"""
    pub = BaijiahaoPublisher()
    content = _content()
    result = asyncio.run(pub.publish(1, {}, content))
    assert result.success is False
    assert "cookies" in (result.error or "")


# ============== _fetch_post_metadata 行为用例 ==============


def test_fetch_post_metadata_returns_zeros_when_card_not_found():
    """页面没渲染出 .article-list-item（结构变 / 文章未入库）→ 返回 None，不抛。

    publish 已成功，不能因为后置抓 metadata 失败而把整个 publish 拖垮。"""
    pub = BaijiahaoPublisher()
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock(side_effect=TimeoutError("article-card timeout"))
    page.evaluate = AsyncMock()  # 不应被调

    result = asyncio.run(pub._fetch_post_metadata(page))

    assert result is None
    page.evaluate.assert_not_awaited()


def test_fetch_post_metadata_returns_none_on_goto_exception():
    """goto 抛网络异常 → 返回 None，不向上传播（异常都拦在底层）。"""
    pub = BaijiahaoPublisher()
    page = MagicMock()
    page.goto = AsyncMock(side_effect=RuntimeError("network boom"))
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock()

    result = asyncio.run(pub._fetch_post_metadata(page))
    assert result is None


def test_fetch_post_metadata_passes_match_post_id_for_collect_path():
    """collect 路径：传 match_post_id，JS 那边按 ?id={id} 匹配。"""
    pub = BaijiahaoPublisher()
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock(return_value={
        "url": "https://baijiahao.baidu.com/s?id=1730009999988887777",
        "view_count": "888",
        "comment_count": "22",
        "like_count": "100",
        "share_count": "",
        "publish_time": "",
    })

    result = asyncio.run(pub._fetch_post_metadata(page, match_post_id="1730009999988887777"))

    assert result is not None
    assert "1730009999988887777" in result["url"]
    eval_args, _ = page.evaluate.call_args
    assert eval_args[1] == {"matchPostId": "1730009999988887777"}


# ============== collect_metrics 行为用例 ==============


def _fake_context():
    ctx = MagicMock()
    ctx.add_cookies = AsyncMock()
    ctx.new_page = AsyncMock(return_value=MagicMock())
    return ctx


def _patch_async_playwright(monkeypatch, fake_p):
    """patch baijiahao.get_async_playwright 让 `async with` 拿到 fake_p。"""
    class _Ctx:
        async def __aenter__(self_inner):
            return fake_p
        async def __aexit__(self_inner, *a):
            return None
    def _factory():
        return lambda: _Ctx()
    monkeypatch.setattr(bjh, "get_async_playwright", _factory)


def test_collect_metrics_returns_zeros_when_credential_missing():
    """凭证缺 cookies → zeros + raw.error，不起浏览器（短路）。"""
    pub = BaijiahaoPublisher()
    result = asyncio.run(pub.collect_metrics("1730009999988887777", None, {}))
    assert result == {"likes": 0, "comments": 0, "shares": 0, "views": 0,
                      "raw": {"error": "凭证缺 cookies"}}


def test_collect_metrics_returns_real_counts_on_match(monkeypatch):
    """命中卡片 → 解析所有数字字段返回真数字，raw 里保留原始字符串供观测。

    这是 ROI 闭环 —— 一次 navigate 抓全字段，不调百家号开放平台数据接口。"""
    pub = BaijiahaoPublisher()

    async def _meta(*_a, **_kw):
        return {
            "url": "https://baijiahao.baidu.com/s?id=1730009999988887777",
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

    credential = {"cookies": [{"name": "BAIDUID", "value": "abc", "domain": ".baidu.com"}]}
    result = asyncio.run(pub.collect_metrics("1730009999988887777", None, credential))

    assert result["views"] == 15000   # 1.5万
    assert result["comments"] == 234
    assert result["likes"] == 3500    # 3.5k
    assert result["shares"] == 12
    assert "1730009999988887777" in result["raw"]["url"]
    # 原始字符串保留供观测
    assert result["raw"]["view_count_raw"] == "1.5万"
    assert result["raw"]["like_count_raw"] == "3.5k"


def test_collect_metrics_returns_zeros_when_post_not_found(monkeypatch):
    """卡片找不到（_fetch_post_metadata 返回 None）→ zeros + raw.not_found=True。

    常见场景：文章已下架 / 被删 / 还没刷出来 —— 老老实实返 zeros 让飞轮继续。"""
    pub = BaijiahaoPublisher()

    async def _none(*_a, **_kw):
        return None
    monkeypatch.setattr(pub, "_fetch_post_metadata", _none)

    fake_browser = MagicMock()
    fake_browser.new_context = AsyncMock(return_value=_fake_context())
    fake_browser.close = AsyncMock()
    fake_p = MagicMock()
    fake_p.chromium.launch = AsyncMock(return_value=fake_browser)
    _patch_async_playwright(monkeypatch, fake_p)

    credential = {"cookies": [{"name": "BAIDUID", "value": "abc"}]}
    result = asyncio.run(pub.collect_metrics("1730009999988887777", None, credential))

    assert result["likes"] == 0
    assert result["comments"] == 0
    assert result["shares"] == 0
    assert result["views"] == 0
    assert result["raw"]["not_found"] is True
    assert result["raw"]["post_id"] == "1730009999988887777"


def test_collect_metrics_returns_zeros_on_playwright_exception(monkeypatch):
    """playwright 启动抛异常 → zeros + raw.error，不向飞轮抛。"""
    pub = BaijiahaoPublisher()

    fake_p = MagicMock()
    fake_p.chromium.launch = AsyncMock(side_effect=RuntimeError("chromium not installed"))
    _patch_async_playwright(monkeypatch, fake_p)

    credential = {"cookies": [{"name": "BAIDUID", "value": "abc"}]}
    result = asyncio.run(pub.collect_metrics("1730009999988887777", None, credential))

    assert result["likes"] == 0
    assert result["views"] == 0
    assert "error" in result["raw"]
    assert "chromium not installed" in result["raw"]["error"]


# ============== bonus：防 substring 反面教训 ==============


def test_publish_button_uses_text_is_not_substring():
    """grep 源码确认发布按钮 selector 用 `:text-is` 不是 `has-text`。

    吸取知乎 publisher 的 has-text 误命中工程坑 ——
    has-text("发布") 会同时命中"发布草稿""再次发布""取消发布"等长文本节点，
    导致点错按钮触发隐藏交互。:text-is 严格精确匹配文本，不会跨节点漂移。

    检查方式：剥掉注释行 + docstring 行后再判断 has-text(，
    避免警示性注释（教未来维护者别用 has-text）被自己的回归测试误伤。
    """
    src_path = Path(__file__).parent.parent / "src" / "ai_ops" / "publishers" / "baijiahao.py"
    src = src_path.read_text(encoding="utf-8")
    # 发布按钮 selector 必须用 :text-is
    assert ':text-is("发布")' in src, "发布按钮 selector 必须用 :text-is 精确匹配（防 has-text 误命中坑）"

    # 防回归：剥掉以 # 开头的注释行 + 整行在 docstring 中的（粗筛——baijiahao.py 没有
    # 复杂的内嵌字符串场景），然后判断是否有 has-text( 真出现在代码逻辑里
    in_docstring = False
    code_lines = []
    for line in src.splitlines():
        stripped = line.strip()
        # docstring 边界（粗略：以 """ 开头/结尾计数）
        if stripped.startswith('"""') or stripped.endswith('"""'):
            # 同行开闭 """...""" 算注释，跳过；纯 """ 切换状态
            if stripped.count('"""') == 2:
                continue
            in_docstring = not in_docstring
            continue
        if in_docstring:
            continue
        # 去掉行末注释
        if stripped.startswith('#'):
            continue
        code_lines.append(line)
    code_only = "\n".join(code_lines)
    assert "has-text(" not in code_only, (
        "百家号 publisher 代码逻辑中不应有 has-text(——必须用 :text-is 精确匹配（防回归）"
    )
