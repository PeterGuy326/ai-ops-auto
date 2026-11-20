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
from pathlib import Path

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
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
        line = line.replace("**", "")
        line = line.replace("`", "")
        # 表格分隔行（|---|---|）不渲染，整行剔除；其余表格行去竖线取单元格文字。
        if re.fullmatch(r"\s*\|?[\s:|-]*-[\s:|-]*\|?\s*", line):
            continue
        line = line.replace("|", "")
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
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


TITLE_SELECTOR = "#title"
DIGEST_SELECTOR = "textarea[name=digest]"
BODY_SELECTOR = "#ueditor_0 .ProseMirror"


async def readback_draft(profile_dir: str, draft_id: str) -> dict | None:
    """UI 导航读回草稿标题/摘要/正文（fail-closed）。复用 Easel 持久化会话。

    导航路径（Phase 1 实测）：首页→「草稿箱」链接→点 tooltip「编辑」→编辑器在新标签页→
    正文 ``#ueditor_0 .ProseMirror``。不直拼编辑器 URL（token 每会话刷新须重取）。
    """
    import asyncio

    return await asyncio.to_thread(_readback_sync, draft_id)


def _readback_sync(draft_id: str) -> dict | None:
    try:
        import sys

        from playwright.sync_api import sync_playwright

        sys.path.insert(0, str(_repo_root() / "vendor/easel/skills/shared/scripts"))
        import weixin_mp_stats as w

        with sync_playwright() as p:
            ctx = w._launch(p, False, None, None)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(w.MP_HOME, wait_until="commit", timeout=60000)
            page.wait_for_timeout(5000)
            href = page.eval_on_selector_all(
                "a[href]",
                "els => { const a = els.find(e => (e.innerText||'').includes('草稿箱')); return a ? a.href : ''; }",
            )
            if not href:
                return None
            page.goto(href, wait_until="commit", timeout=60000)
            page.wait_for_timeout(9000)
            clicked = page.evaluate(
                """() => {
                    const tips=[...document.querySelectorAll('.weui-desktop-card__action .weui-desktop-tooltip')];
                    const t=tips.find(e=>(e.textContent||'').trim()==='编辑'); if(!t) return false;
                    const wrp=t.closest('.weui-desktop-tooltip__wrp')||t.parentElement;
                    (wrp.querySelector('a')||wrp).click(); return true; }"""
            )
            if not clicked:
                return None
            page.wait_for_timeout(3000)
            ed = ctx.pages[-1]
            try:
                ed.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass
            ed.wait_for_timeout(12000)
            ce = ed.query_selector(BODY_SELECTOR) or ed.query_selector("[contenteditable='true']")
            body = ce.inner_text() if ce else ""
            ti = ed.query_selector(TITLE_SELECTOR)
            di = ed.query_selector(DIGEST_SELECTOR)
            result = {
                "title": ti.input_value() if ti else "",
                "digest": di.input_value() if di else "",
                "body": body,
                "draft_id": draft_id,
            }
            ctx.close()
            return result
    except Exception:
        return None
