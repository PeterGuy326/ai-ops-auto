"""Boss 直聘适配器 —— 自建 Playwright 实现（对照 publishers/zhihu.py 的自建模式）。

⚠️ P2 激活：本文件的浏览器自动化**需要真 Boss 登录态**才能真跑，
当前环境无账号/无浏览器/Boss 反爬，无法在 CI 离线验证——这点与现有
publishers（zhihu/toutiao 等）一致：抽象与装配可单测，真发布/真爬取靠真账号集成验证。
离线想跑通整条候选池管道，用 appliers/fake.FakeApplier（`jobhunt crawl-match --fake`）。

凭证格式（P2 经 accounts/store.py Fernet 加密落库后解密传入 credential）：
  {"cookies": [{"name": "...", "value": "...", "domain": ".zhipin.com", "path": "/"}, ...]}

Boss 的特殊性（决定 apply 与表单式平台不同）：
  投递 = 在岗位详情页点「立即沟通」→ 进聊天 → 发打招呼语 → 跟 HR 一来一回。
  因此 apply 是「发 greeting」，HR 后续回复靠 poll_replies（P3）追踪。
"""
from __future__ import annotations

import asyncio
import hashlib
import random

from ..enums import JobBoard
from ..schemas import ApplyResult, JobCandidate, JobQuery
from .base import ApplierBase

SEARCH_URL = "https://www.zhipin.com/web/geek/job"
LOGIN_URL = "https://www.zhipin.com/web/user/?ka=header-login"

# 真环境校准（2026-06 真账号 CDP 联调，www.zhipin.com 搜索结果页 SPA）
SEL_SEARCH_INPUT = "input.ipt-search[name=query]"
SEL_SEARCH_BTN = "a.btn-search, .btn-search"
SEL_JOB_CARD = "li.job-card-box"
SEL_JOB_TITLE = "a.job-name"
SEL_COMPANY = ".boss-name"
SEL_SALARY = ".job-salary"
SEL_LOCATION = ".company-location"
SEL_LOGGED_IN = ".nav-figure, [ka=header-username]"
HOME_URL = "https://www.zhipin.com/"

# —— 聊天式投递（apply）相关选择器（2026-06 真账号校准）——
# 详情页「立即沟通」按钮（已沟通过变「继续沟通」）。用 a.btn-startchat，点击弹出页内模态框。
# 注意：DOM 里有 0×0 的隐藏同名节点，点击要取「可见」那个（locator.first + visible）。
SEL_START_CHAT = "a.btn-startchat"
# 点「立即沟通」后弹出的页内会话模态框（不是跳 /web/geek/chat）
SEL_CHAT_DIALOG = ".startchat-dialog"
SEL_DIALOG_INPUT = ".startchat-dialog textarea.input-area"  # 招呼语输入框
SEL_DIALOG_SEND = ".startchat-dialog .send-message"          # 发送按钮
SEL_DIALOG_CLOSE = ".startchat-dialog .close"                # 关闭（清残留模态框）
# 发送成功后模态框消息区渲染出我方气泡（.message 内含我的招呼语文本）
SEL_DIALOG_MSG = ".startchat-dialog .message"
# ⚠️ Boss 在模态框打开时会自动先发一条通用模板「您好，非常喜欢…」（平台行为，无法阻止）；
# 我们的定制招呼语是随后补发的第二条。
# 命中这些文案 = 投递被拦（需补全信息 / 今日已达上限 / 风控验证）
BLOCK_HINTS = ("完善简历", "完善信息", "今日沟通", "达到上限", "安全验证", "滑动验证", "频繁")


def _stable_id(url: str) -> str:
    """平台没给稳定 id 时，用 url 的 hash 兜底，保证 (board, external_id) 去重稳定。"""
    return "url-" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


async def _human_delay(lo: float = 1.0, hi: float = 3.0) -> None:
    """关键操作间插入随机延迟，模拟真人节奏，降低 Boss 风控触发概率（对齐 zhihu publisher）。"""
    await asyncio.sleep(random.uniform(lo, hi))


class BossApplier(ApplierBase):
    board = JobBoard.BOSS

    def __init__(self, *, headless: bool | None = None):
        # 反爬：高风控平台建议非无头（与现有 settings.browser_headless 一致）
        self._headless = headless

    async def search_jobs(
        self, query: JobQuery, *, credential: dict | None = None
    ) -> list[JobCandidate]:
        """打开 Boss 搜索页，按关键词+城市抓岗位卡 → JobCandidate[]。

        登录态两种来源（browser.open_page 统一处理）：
          - CDP 模式（settings.browser_cdp_url）：复用你本人已登录 Boss 的真 Chrome，免 cookie。
          - 自启模式：需 credential.cookies 注入，否则只能拿游客有限结果或被拦。
        离线演练请改用 FakeApplier（jobhunt crawl-match --fake）。
        """
        from ..browser import cdp_enabled, open_page

        if not cdp_enabled() and not (credential and credential.get("cookies")):
            raise RuntimeError(
                "BossApplier.search_jobs 需要登录态：配 BROWSER_CDP_URL 走真 Chrome，"
                "或传 credential.cookies；离线演练用 FakeApplier。"
            )

        keyword = (query.keywords or [""])[0]
        out: list[JobCandidate] = []
        async with open_page(credential, headless=self._headless) as page:
            # Boss SPA 的搜索结果深链直接 goto 会被弹回首页，必须走首页搜索框 UI 触发。
            # 已在 zhipin 域内就别重复 goto（少一次导航少一次风控触发）；只有不在站内才回首页。
            cur = page.url or ""
            if "zhipin.com" not in cur or "/web/geek/jobs" in cur:
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=60000)
                await _human_delay(1.5, 2.5)
            await page.wait_for_selector(SEL_SEARCH_INPUT, timeout=15000)
            # 拟人：hover→click→逐字打字（selector 级，避免 handle 失效）
            try:
                await page.hover(SEL_SEARCH_INPUT)
            except Exception:
                pass
            await _human_delay(0.2, 0.6)
            await page.click(SEL_SEARCH_INPUT)
            await _human_delay(0.3, 0.8)
            await page.fill(SEL_SEARCH_INPUT, "")
            for ch in keyword:
                await page.type(SEL_SEARCH_INPUT, ch, delay=random.randint(70, 190))
            await _human_delay(0.6, 1.4)
            try:
                async with page.expect_navigation(timeout=20000):
                    await page.click(SEL_SEARCH_BTN)
            except Exception:
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    pass

            # 结果页异步渲染，轮询等岗位卡出现
            try:
                await page.wait_for_selector(SEL_JOB_CARD, timeout=20000)
            except Exception:
                return out  # 没出卡（关键词无结果/被风控），返回空让上层据实处理

            cards = await page.query_selector_all(SEL_JOB_CARD)
            for card in cards[: query.limit]:
                cand = await self._parse_card(card)
                if cand:
                    out.append(cand)
        return out

    async def _parse_card(self, card) -> JobCandidate | None:
        """从一张岗位卡抽字段。失败返回 None（单卡异常不拖垮整页采集）。"""
        async def _text(sel: str) -> str:
            el = await card.query_selector(sel)
            return (await el.inner_text()).strip() if el else ""

        try:
            title = await _text(SEL_JOB_TITLE)
            link_el = await card.query_selector(SEL_JOB_TITLE)  # a.job-name 带 /job_detail/ 链接
            href = await link_el.get_attribute("href") if link_el else ""
            url = href if href.startswith("http") else f"https://www.zhipin.com{href}"
            # 优先用 /job_detail/<id>.html 里的稳定 id 去重，拿不到再回退 url hash
            ext_id = _stable_id(url)
            if "/job_detail/" in href:
                token = href.split("/job_detail/")[-1].split(".html")[0].split("?")[0]
                if token:
                    ext_id = token
            return JobCandidate(
                board=JobBoard.BOSS,
                external_id=ext_id,
                url=url,
                title=title,
                company=await _text(SEL_COMPANY),
                location=await _text(SEL_LOCATION),
                salary_text=await _text(SEL_SALARY),
                jd_text="",  # 列表页无完整 JD；P2 可点进详情页补全
            )
        except Exception:
            return None

    async def apply(
        self, *, credential: dict, job: JobCandidate, resume_summary: str, greeting: str
    ) -> ApplyResult:
        """聊天式投递：进岗位详情页 → 点「立即沟通」→ 在会话里发打招呼语。

        Boss 没有「提交简历」这种一锤子动作，投递 = 发出第一条招呼语并进 HR 会话。
        因此判「成功」的口径是：招呼语真的发进了会话区（看到我方气泡），
        而不是「点了按钮没报错」——后者会造成 zhihu.py 注释里说的那种「虚假闭环」。

        失败/被拦统一回 ApplyResult(success=False, error=...)，由编排层决定重试/置 DEAD。
        异常不外抛（单次投递失败不该拖垮整批）。
        """
        from ..browser import cdp_enabled, open_page

        if not cdp_enabled() and not (credential and credential.get("cookies")):
            return ApplyResult(success=False, error="缺登录态（配 BROWSER_CDP_URL 或传 cookies）")
        if not job.url:
            return ApplyResult(success=False, error="岗位无 url，无法进详情页沟通")
        if not greeting.strip():
            return ApplyResult(success=False, error="招呼语为空，拒绝投递")

        try:
            async with open_page(credential, headless=self._headless) as page:
                return await self._do_apply(page, job, greeting)
        except Exception as e:  # 浏览器层异常兜底，不外抛
            return ApplyResult(success=False, error=f"Boss 投递异常: {e}")

    async def _do_apply(self, page, job: JobCandidate, greeting: str) -> ApplyResult:
        """单页投递流程：进详情页 → 点「立即沟通」→ 在弹出的 startchat 模态框里发定制招呼语。

        Boss 实测流程（2026-06）：
          - 点「立即沟通」弹出页内模态框 `.startchat-dialog`，且平台**自动先发**一条通用模板。
          - 模态框里 `textarea.input-area` 填我们的定制招呼语，点 `.send-message` 发出（第二条）。
          - 成功判据：定制招呼语文本出现在模态框消息区（防虚假闭环）。
        """
        await page.goto(job.url, wait_until="domcontentloaded", timeout=60000)
        await _human_delay(2, 4)

        # 先清掉可能残留的上一单模态框（满屏覆盖会挡住本单的「立即沟通」按钮）
        await self._dismiss_dialog(page)

        # 风控/拦截文案早判
        body_text = (await page.inner_text("body")) if await page.locator("body").count() else ""
        for hint in BLOCK_HINTS:
            if hint in body_text:
                return ApplyResult(success=False, error=f"被平台拦截/需人工处理：命中『{hint}』")

        # 定位可见的沟通按钮（DOM 有 0×0 隐藏同名节点）
        chat_btn = page.locator(f"{SEL_START_CHAT}:visible").first
        try:
            await chat_btn.wait_for(state="visible", timeout=15000)
        except Exception:
            return ApplyResult(
                success=False,
                error="未找到可见「立即沟通」按钮（岗位下架/选择器需校准/未登录）",
                url=page.url,
            )

        # 「继续沟通」= 这单先前已建立会话（Boss 至少发过模板）。不重复打扰 HR，直接判已联系。
        btn_text = (await chat_btn.inner_text()).strip()
        if "继续" in btn_text:
            return ApplyResult(
                success=True, external_id=job.external_id, url=page.url,
                raw={"note": "already_contacted", "greeting_len": len(greeting)},
            )

        await _human_delay(1, 2)
        await chat_btn.click()

        # 等模态框输入框出现（点完平台会自动发模板，模态框随之弹出）
        try:
            await page.wait_for_selector(SEL_DIALOG_INPUT, timeout=20000)
        except Exception:
            return ApplyResult(
                success=False,
                error="点了沟通但没弹出会话模态框（可能被风控/选择器需校准）",
                url=page.url,
            )

        # 幂等护栏：若定制招呼语已在模态框消息区，说明这单先前发过，别重复发
        if await self._greeting_already_sent(page, greeting):
            return ApplyResult(
                success=True, external_id=job.external_id, url=page.url,
                raw={"greeting_len": len(greeting), "note": "already_sent"},
            )

        # 填定制招呼语：聚焦 → 全选删除清空 → 逐字 type
        await page.click(SEL_DIALOG_INPUT)
        await _human_delay(0.4, 1.0)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await _human_delay(0.2, 0.5)
        await page.type(SEL_DIALOG_INPUT, greeting, delay=random.randint(20, 60))
        await _human_delay(0.8, 1.6)

        # 点发送
        try:
            await page.click(SEL_DIALOG_SEND, timeout=8000)
        except Exception:
            return ApplyResult(success=False, error="找不到/点不动发送按钮", url=page.url)

        # 闭环：等定制招呼语真出现在模态框消息区
        await _human_delay(1.5, 3)
        if not await self._greeting_already_sent(page, greeting):
            return ApplyResult(
                success=False,
                error="招呼语已提交但未在会话区确认（可能被风控静默拦截）",
                url=page.url,
            )

        await self._dismiss_dialog(page)  # 收尾关掉模态框，免得挡下一单
        return ApplyResult(
            success=True,
            external_id=job.external_id,
            url=page.url,
            raw={"greeting_len": len(greeting)},
        )

    @staticmethod
    async def _dismiss_dialog(page) -> None:
        """关掉打开着的 startchat 模态框（不报错）。"""
        try:
            close = page.locator(f"{SEL_DIALOG_CLOSE}:visible").first
            if await close.count():
                await close.click(timeout=3000)
                await _human_delay(0.3, 0.8)
        except Exception:
            pass

    @staticmethod
    async def _greeting_already_sent(page, greeting: str) -> bool:
        """定制招呼语是否已出现在模态框消息区（幂等 + 闭环判定共用）。"""
        try:
            return await page.evaluate(
                """(g) => {
                    const nodes = document.querySelectorAll('.startchat-dialog .message, .startchat-dialog .text');
                    return Array.from(nodes).some(n => (n.textContent || '').includes(g.slice(0, 12)));
                }""",
                greeting,
            )
        except Exception:
            return False


