"""WechatMpPublisher 单测 — 全 mock，不依赖真账号 / 真浏览器。

测试目标：_do_publish(page, content) 私有方法，覆盖：
  1. 成功路径（标题 + 正文 + 封面 + 保存草稿 → success=True）
  2. 标题为空 → 提前返回 success=False
  3. 编辑器找不到（wait_for_selector 抛 TimeoutError）→ success=False
  4. 封面上传失败（set_input_files 抛异常）→ success=False
  5. paste 失败（evaluate 抛异常）→ success=False

并附带 selector 常量存在性 / 不实现 send-draft 的静态检查作为兜底。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ai_ops.core.enums import ContentType, Platform, PublisherKind
from ai_ops.core.schemas import PublishContent, PublishResult
from ai_ops.publishers import wechat_mp as wmp
from ai_ops.publishers.wechat_mp import (
    COVER_UPLOAD_INPUT_SELECTOR,
    EDITOR_FRAME_SELECTOR,
    SAVE_DRAFT_BUTTON_SELECTOR,
    TITLE_INPUT_SELECTOR,
    CONTENT_EDITOR_SELECTOR,
    WechatMpPublisher,
)


# ============== 全局：把 _random_delay 实际睡眠 patch 掉，让单测秒过 ==============


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """生产代码用 _random_delay/asyncio.sleep 模拟真人节奏，单测不需要真睡。"""
    async def _noop(*_a, **_kw):
        return None
    monkeypatch.setattr(wmp.asyncio, "sleep", _noop)


# ============== 辅助：mock playwright page + frame_locator 链 ==============


# ============== 辅助：mock playwright page + frame_locator 链 ==============


def _build_mock_locator(*, click_raise: Exception | None = None,
                       fill_raise: Exception | None = None,
                       set_input_raise: Exception | None = None) -> MagicMock:
    """构造 locator chain：locator(...).first.{fill, click, set_input_files, count}"""
    loc = MagicMock()
    first = MagicMock()
    first.fill = AsyncMock(side_effect=fill_raise) if fill_raise else AsyncMock()
    first.click = AsyncMock(side_effect=click_raise) if click_raise else AsyncMock()
    first.set_input_files = (
        AsyncMock(side_effect=set_input_raise) if set_input_raise else AsyncMock()
    )
    loc.first = first
    loc.count = AsyncMock(return_value=1)
    return loc


def _build_mock_editor_root(*,
                            wait_raise: Exception | None = None,
                            click_raise: Exception | None = None,
                            fill_raise: Exception | None = None,
                            set_input_raise: Exception | None = None) -> MagicMock:
    """editor_root 是 frame_locator 或 page，对外暴露 wait_for_selector + locator"""
    root = MagicMock()
    root.wait_for_selector = (
        AsyncMock(side_effect=wait_raise) if wait_raise else AsyncMock()
    )
    # locator 不区分 selector，统一返回 _build_mock_locator
    root.locator = MagicMock(side_effect=lambda sel: _build_mock_locator(
        click_raise=click_raise,
        fill_raise=fill_raise,
        set_input_raise=set_input_raise,
    ))
    return root


def _build_mock_page(editor_root: MagicMock, *,
                     evaluate_raise: Exception | None = None,
                     final_url: str = "https://mp.weixin.qq.com/cgi-bin/appmsgpublish?appmsgid=999&action=edit") -> MagicMock:
    page = MagicMock()
    page.goto = AsyncMock()
    page.url = final_url
    # frame_locator 返回 editor_root（让 _resolve_editor_root 走 iframe 分支）
    page.frame_locator = MagicMock(return_value=editor_root)
    page.evaluate = (
        AsyncMock(side_effect=evaluate_raise) if evaluate_raise else AsyncMock()
    )
    return page


# ============== 用例 ==============


def test_selectors_defined():
    """selector 常量必须存在且非空——publishing-sop §三-C 要求集中在顶部。"""
    for sel in (
        EDITOR_FRAME_SELECTOR,
        TITLE_INPUT_SELECTOR,
        CONTENT_EDITOR_SELECTOR,
        COVER_UPLOAD_INPUT_SELECTOR,
        SAVE_DRAFT_BUTTON_SELECTOR,
    ):
        assert isinstance(sel, str) and sel


def test_save_draft_selector_does_not_match_mass_send():
    """保存草稿按钮 selector 必须用精确匹配，杜绝命中「群发」类按钮。"""
    # 必须是 text-is 精确匹配，不能用 has-text substring（避免误命中"群发"含字）
    assert ':text-is(' in SAVE_DRAFT_BUTTON_SELECTOR
    # 严禁出现群发/发送类关键字
    forbidden = ("群发", "send-draft", "发送给", "mass-send", "publish-all")
    for kw in forbidden:
        assert kw not in SAVE_DRAFT_BUTTON_SELECTOR, f"selector 不能命中 {kw}"


def test_metadata():
    pub = WechatMpPublisher()
    assert pub.platform == Platform.WECHAT_MP
    assert pub.kind == PublisherKind.SOCIAL_AUTO_UPLOAD


# ----- _do_publish 行为用例 -----


def _content(title: str = "测试标题", body: str = "正文 **加粗** [link](http://x)",
             images: list[str] | None = None) -> PublishContent:
    return PublishContent(
        title=title,
        body=body,
        content_type=ContentType.IMAGE_TEXT,
        images=images or [],
    )


def test_do_publish_success_path(tmp_path):
    """成功路径：标题 + 正文 + 封面 + 保存草稿全过且 platform_url 抓回。"""
    pub = WechatMpPublisher()
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\xff\xd8\xff\xe0")  # 任意非空字节，set_input_files 是 mock
    editor_root = _build_mock_editor_root()
    page = _build_mock_page(editor_root)

    result: PublishResult = asyncio.run(pub._do_publish(page, _content(images=[str(cover)])))

    assert result.success is True
    assert result.platform_url
    assert result.platform_post_id == "999"  # 从 URL 抽 appmsgid
    assert result.raw_response["cover_uploaded"] is True
    assert result.raw_response["stage"] == "draft_only"
    page.goto.assert_awaited_once()
    page.evaluate.assert_awaited()  # paste evaluate 被调


def test_do_publish_empty_title_returns_failure():
    """标题为空：提前 fail，不进任何浏览器操作。"""
    pub = WechatMpPublisher()
    editor_root = _build_mock_editor_root()
    page = _build_mock_page(editor_root)

    result = asyncio.run(pub._do_publish(page, _content(title="   ")))

    assert result.success is False
    assert "标题为空" in (result.error or "")
    page.goto.assert_not_awaited()


def test_do_publish_editor_not_found_returns_failure():
    """编辑器 selector 找不到：wait_for_selector 抛 TimeoutError → 明确错误。"""
    pub = WechatMpPublisher()
    editor_root = _build_mock_editor_root(wait_raise=TimeoutError("title timeout"))
    page = _build_mock_page(editor_root)

    result = asyncio.run(pub._do_publish(page, _content()))

    assert result.success is False
    assert "未找到 mp 标题输入框" in (result.error or "")


def test_do_publish_cover_upload_failure_returns_failure(tmp_path):
    """封面上传失败：set_input_files 抛异常 → 失败（mp 封面必填）。"""
    pub = WechatMpPublisher()
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"\x00")
    editor_root = _build_mock_editor_root(set_input_raise=RuntimeError("upload boom"))
    page = _build_mock_page(editor_root)

    result = asyncio.run(pub._do_publish(page, _content(images=[str(cover)])))

    assert result.success is False
    assert "上传封面失败" in (result.error or "")


def test_do_publish_paste_failure_returns_failure():
    """paste evaluate 抛异常 → 正文粘贴失败。"""
    pub = WechatMpPublisher()
    editor_root = _build_mock_editor_root()
    page = _build_mock_page(editor_root, evaluate_raise=RuntimeError("paste boom"))

    result = asyncio.run(pub._do_publish(page, _content()))

    assert result.success is False
    assert "正文粘贴失败" in (result.error or "")


def test_do_publish_video_content_type_returns_failure_via_publish():
    """video 类型应在 publish() 入口拦截，不走 _do_publish。"""
    pub = WechatMpPublisher()
    content = PublishContent(
        title="t",
        body="b",
        content_type=ContentType.VIDEO,
    )

    result = asyncio.run(pub.publish(1, {"profile_dir": "/nonexistent"}, content))
    assert result.success is False
    assert "视频号" in (result.error or "")


def test_publish_missing_profile_dir_returns_failure():
    """credential 没 profile_dir 或目录不存在 → 提示去 login。"""
    pub = WechatMpPublisher()
    content = _content()

    result = asyncio.run(pub.publish(1, {}, content))
    assert result.success is False
    assert "profile_dir" in (result.error or "")


def test_extract_draft_id():
    """URL → draft id 抽取兼容 appmsgid/mid/draft_id 三种 key。"""
    f = WechatMpPublisher._extract_draft_id
    assert f("https://mp.weixin.qq.com/x?appmsgid=12345&t=1") == "12345"
    assert f("https://mp.weixin.qq.com/x?mid=678") == "678"
    assert f("https://mp.weixin.qq.com/x?draft_id=42&action=edit") == "42"
    assert f("https://mp.weixin.qq.com/x") is None
    assert f("") is None
