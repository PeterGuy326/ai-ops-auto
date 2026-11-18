"""公众号草稿逐字读回 — 把 Phase 1 实测摸通的读回通道固化为可复用模块。

读回通道（2026-11-18 实测，evidence-card-001）：
  * 草稿箱服务端内嵌 ``window.wx.cgiData`` → 标题/摘要（逐字）。
  * 编辑器须全程 UI 导航进 ``appmsg_edit``（直拼 URL/旧 token 报「请重新登录」，
    token 每会话刷新须重取）；正文在 ``#ueditor_0 .ProseMirror``（UEditor 内联）。
  * 预览临时链接触发微信环境验证页，**不可**做读回通道；GET 接口被 CSRF 拦。

比对纪律：剔除源文件 META 编辑备注（HTML 注释）；代码围栏内不做 markdown 剥离
（否则 ``# 注释`` 被误当标题剥掉）。本模块的纯比对逻辑可离线单测；浏览器提取
fail-closed，任何异常返回 None，绝不伪造读回结果。
"""
from __future__ import annotations

import re

_META_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCE_RE = re.compile(r"^(```|~~~)")


def strip_meta_comments(md: str) -> str:
    return _META_COMMENT_RE.sub("", md)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


def split_paragraphs(md: str) -> list[str]:
    """按空行分段；代码围栏整体作为一段，围栏内不剥离 markdown 符号。"""
    md = strip_meta_comments(md)
    paras: list[str] = []
    buf: list[str] = []
    in_fence = False
    for line in md.splitlines():
        if _FENCE_RE.match(line.strip()):
            in_fence = not in_fence
            if not in_fence:
                paras.append("\n".join(buf))
                buf = []
            continue
        if in_fence:
            buf.append(line)
            continue
        if not line.strip():
            if buf:
                paras.append("\n".join(buf))
                buf = []
            continue
        # 围栏外剥离行首 markdown 标记（标题/列表/引用），保留正文文字。
        buf.append(re.sub(r"^\s*(#{1,6}\s+|[-*+]\s+|>\s?|\d+\.\s+)", "", line))
    if buf:
        paras.append("\n".join(buf))
    return [p for p in (_normalize(x) for x in paras) if p]


def compare_article(
    source_md: str,
    readback_title: str,
    readback_digest: str,
    readback_body: str,
    source_title: str | None = None,
    source_digest: str | None = None,
) -> dict:
    """逐字比对标题/摘要/正文段落，返回可复核的匹配报告（不伪造）。"""
    src_paras = split_paragraphs(source_md)
    rb_norm = _normalize(readback_body)
    mismatches = []
    matched = 0
    for i, para in enumerate(src_paras):
        if para and para in rb_norm:
            matched += 1
        else:
            mismatches.append({"index": i, "source": para[:80]})
    title_match = source_title is None or _normalize(source_title) == _normalize(readback_title)
    digest_match = source_digest is None or _normalize(source_digest) == _normalize(readback_digest)
    return {
        "title_match": bool(title_match),
        "digest_match": bool(digest_match),
        "body_matched": matched,
        "body_total": len(src_paras),
        "body_all_match": matched == len(src_paras),
        "mismatches": mismatches,
    }


# ---------------- 浏览器提取（fail-closed，须真实会话）----------------
TITLE_SELECTOR = "#title"
DIGEST_SELECTOR = "textarea[name=digest]"
BODY_SELECTOR = "#ueditor_0 .ProseMirror"


async def readback_draft(profile_dir: str, draft_id: str) -> dict | None:
    """UI 导航进草稿编辑器提取标题/摘要/正文。任何异常返回 None（fail-closed）。

    复用 Easel 持久化 profile（~/.easel-browser-profiles/WeixinMpProfile）。
    导航路径：home → 草稿箱 → 编辑按钮（图标 tooltip），不直拼编辑器 URL。
    """
    try:
        from ..runtime.playwright_factory import get_async_playwright, get_launch_kwargs

        async_playwright = get_async_playwright()
        kwargs = get_launch_kwargs()
        channel = kwargs.pop("channel", None)
        proxy = kwargs.pop("proxy", None)
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                user_data_dir=profile_dir, channel=channel, proxy=proxy,
                viewport={"width": 1440, "height": 900}, **kwargs,
            )
            try:
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto("https://mp.weixin.qq.com/", timeout=60000)
                # 草稿箱 → 编辑（UI 导航，token 由会话自带）
                await page.goto(
                    "https://mp.weixin.qq.com/cgi-bin/appmsgpublish?sub=list&search_field=null"
                    "&begin=0&count=5&query=&type=77&token=[REDACTED]",
                    timeout=60000,
                )
                await page.wait_for_selector(".weui-desktop-card", timeout=30000)
                edit = page.locator("[title='编辑'], a:has-text('编辑')").first
                await edit.click()
                await page.wait_for_selector(BODY_SELECTOR, timeout=30000)
                title = await page.locator(TITLE_SELECTOR).input_value()
                digest = await page.locator(DIGEST_SELECTOR).input_value()
                body = await page.locator(BODY_SELECTOR).inner_text()
                return {"title": title, "digest": digest, "body": body, "draft_id": draft_id}
            finally:
                await ctx.close()
    except Exception:
        return None