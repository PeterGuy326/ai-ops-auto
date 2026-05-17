# Publishing SOP（ai-ops-auto 发布层）

> **发布层抓手 = `PublisherBase` 抽象 + 多平台子类（`ZhihuPublisher` / `ToutiaoPublisher` / `WechatMpPublisher` / `SocialAutoUploadPublisher` / `XhsSkillsPublisher` / `GitHubPagesPublisher`）**。
> 浏览器自动化全部委托给 Playwright / patchright（drop-in 反检测 fork），不重写底层；高风控平台留 Publisher fallback 链作为加固档。
>
> 端到端验证：xhs 单篇真发 **27 秒**（cookie 注入 → 上传图 → 填表 → 发布全自动，后台不抢焦点）。

## 一、为什么走 Publisher + Playwright/patchright，不重写 Chrome MCP

| 维度 | 自研 Chrome MCP（废弃） | Publisher (Playwright / patchright) |
|---|---|---|
| 浏览器栈 | 控用户主 Chrome | 独立 Chromium / 真 Chrome channel / patchright stealth fork（按 `settings.browser_engine` 切换） |
| 文件上传 | cliclick + NSOpenPanel + keystroke 路径（**8 轮死磕未通**） | `page.set_input_files(path)` 一行调用 |
| 焦点 | 必须 activate 抢前台 | **完全后台**（headless 或 headed 都不抢焦点） |
| 跨平台 | 只 macOS | macOS / Linux / Windows |
| 反爬维护 | selectors.yaml 每周修 | patchright 上游负责浏览器指纹层；Publisher 子类只维护薄 selector 层 |
| 多实现 fallback | 没有 | `default_registry` 按 priority 路由，主力失败自动 fallback 到加固档 |

> **底层逻辑**：把发布层重写一遍 = 把 social-auto-upload / patchright 已有的几年工程经验丢掉。优先站在巨人肩膀上，自己只在开源缺口（知乎 / 头条 / 公众号）写薄 Publisher。

## 二、统一发布流程

```
[1] 上游编排：Article 状态机推进到 READY / SCHEDULED
    POST /articles {topic_id, title, body, content_type, target_account_ids}
        ↓
[2] scheduler 把 (article, account) 笛卡尔积成 PublishJob 落库
    （也可手动 POST /jobs/{id}/run 触发）
        ↓
[3] 发布前 grep 兜底（污点教训）
    grep -rE "TODO|过期版本号|未替换占位符" <article.body>  → 任一命中即 fail-fast
    （目前作为运营纪律执行，TODO: 接入到 worker 前置 hook）
        ↓
[4] worker.execute_job
    ├─ check_rate_limit（养号期 + 间隔 + 单日上限）
    ├─ get_credential（Fernet 解密 Account.encrypted_credential）
    ├─ default_registry.resolve(platform) → 拿到按优先级排序的 Publisher 列表
    └─ 依次 publisher.publish(account_id, credential, content)，第一个成功即返回
        ↓
[5] Publisher 内部：打开浏览器 → 注入 cookies → 上传图 → 填表 → 点发布 → 退出
    （Playwright/patchright 全自动，30 秒搞定，无中途暂停）
        ↓
[6] 回写 PublishJob：status=SUCCESS, platform_post_id, platform_url
    Article.status = PUBLISHED
    triggers schedule_after_publish(job.id) → 1h/24h/7d 数据采集飞轮
```

> **历史变化**：早期 spec 强制"发布按钮永远人工点"作为防误发铁律。后来验证 Publisher 是全自动，不在中途停。**误发风险通过"发布前 grep 兜底" + "失败立刻重发覆盖" 两条来兜底**，不再依赖人工点按钮。

## 三、外部依赖装机 + Publisher 调用契约

### 一次性装机（每台新机器）

```bash
# 1. ai-ops-auto 本身
cd /path/to/ai-ops-auto
uv venv --python 3.12 && . .venv/bin/activate
uv pip install -e .

# 2. 浏览器引擎（默认 playwright_chrome_channel = 复用本机真 Chrome）
PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright" playwright install chromium

# 3. 反检测 fork（高风控平台用，drop-in 替换 playwright）
uv pip install patchright
PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright" patchright install chromium

# 4. social-auto-upload 上游（SAU 主力——覆盖小红书/抖音/B站/快手/视频号/TikTok/YouTube 7 个平台）
git clone --depth 1 https://github.com/dreammis/social-auto-upload.git external/social-auto-upload
# settings.external_sau_path 指向该目录，由 SocialAutoUploadPublisher 子进程调用

# 5. 凭证加密密钥
export FERNET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
```

切换浏览器引擎：改 `settings.browser_engine`，可选值：
- `playwright_chromium`：上游 Chromium，最干净
- `playwright_chrome_channel`：复用本机真 Chrome（默认；指纹更真实）
- `patchright`：drop-in 反检测 fork（推荐用于知乎 / 小红书 / 头条等高风控平台）
- `camoufox`：基于 Firefox 的反检测引擎（指纹最强，但 API 不兼容 Playwright，需业务代码独立用 `AsyncCamoufox`）

### Publisher 调用契约（所有平台共用 `PublisherBase`）

所有 Publisher 子类实现这 4 个方法：

| 方法 | 用途 | 调用入口 |
|---|---|---|
| `login(account_id, credential)` | 触发登录（开窗扫码 / OAuth / 持久化 profile），凭证写回 `credential` dict | `POST /accounts/{id}/login` |
| `publish(account_id, credential, content)` | 单次发布，`content` 是平台无关的 `PublishContent` | `POST /jobs/{id}/run` → `worker.execute_job` |
| `health_check(account_id, credential)` | 登录态 / 风控感知，返回 `AccountHealth` | `scheduler.health.check_all_accounts`（默认每天 02:00） |
| `collect_metrics(post_id, post_url, credential)` | 数据采集（按需 override，默认返回 0） | `scheduler.metrics.collect_one` / `POST /jobs/{id}/collect` |

调用方都通过 `default_registry.resolve(Platform.XXX)` 拿 Publisher 列表（按 priority 排序），主力失败自动 fallback 到下一档。

### 支持的平台 / Publisher 对应表

| Platform | 主力 Publisher（priority） | 加固 / fallback Publisher | 备注 |
|---|---|---|---|
| `XIAOHONGSHU` | `SocialAutoUploadPublisher`（10） | `XhsSkillsPublisher`（20） | SAU 主力，高风控用加固档 |
| `DOUYIN` / `BILIBILI` / `KUAISHOU` / `WECHAT_VIDEO` / `TIKTOK` / `YOUTUBE` | `SocialAutoUploadPublisher`（10） | — | 全部走 SAU 上游 |
| `ZHIHU` | `ZhihuPublisher`（10） | — | 开源缺口，自建 |
| `TOUTIAO` | `ToutiaoPublisher`（10） | — | 开源缺口，自建 |
| `WECHAT_MP` | `WechatMpPublisher`（10） | — | 开源缺口，persistent_context 路径 |
| `GITHUB_PAGES` | `GitHubPagesPublisher`（10） | — | 自有博客（Hexo/Jekyll/Hugo） |

## 三-A、头条号 Publisher（自建，开源缺口）

> **底层逻辑**：social-auto-upload 上游 `uploader/` 目录**没有 toutiao_uploader**（只覆盖 xhs / 抖音 / B 站 / 快手 / 视频号 / TikTok / 百家号）。gh search 多关键词穷尽，唯二命中（`axdlee/toutiao-publish` 4⭐、`OceanBBBBbb/auto_write_toutiaohao` 6⭐）都不够成熟。所以**走 PublisherBase + Playwright/patchright 自建**，与其他 Publisher 同形态、同 `runtime/playwright_factory` 反检测能力。

### 接入位置 + 调用契约

```
src/ai_ops/
└── publishers/
    └── toutiao.py          # ToutiaoPublisher 单文件实现：login / publish / health_check
```

- 注册：`publishers/registry.py` → `reg.register(Platform.TOUTIAO, ToutiaoPublisher, priority=10)`
- 触发：`POST /accounts/{id}/login`（首登扫码）/ `POST /jobs/{id}/run`（发布）
- 凭证：`{"cookies": [...]}`（Playwright cookies list），由 `accounts/store.py` Fernet 加密落 `Account.encrypted_credential`，**不落任何文件**

### 头条号自动化的 4 个工程坑（**新平台 Publisher 必查清单**）

> 实施时 6 次发布才闭环，每个坑都是**没有看真实 DOM、凭文档想象**导致的。固化在这里，也已在 `toutiao.py` 注释中标注。

| # | 坑 | 真相 | 修法 |
|---|---|---|---|
| 1 | 点 `.article-cover-add` 没反应 | **必须先填标题+正文**，cover 抽屉才会弹 | Publisher 顺序固定：title → content → cover |
| 2 | cover 触发的不是 file picker | 弹出**全屏抽屉**（`.byte-drawer-wrapper`），抽屉里有 hidden `input[type=file]` | `set_input_files(.upload-image-panel input[type=file])` + 点抽屉「确定」按钮（`size-large` 不是 `size-huge`），不要走 `expect_file_chooser` |
| 3 | 「预览并发布」点了但作品管理后台没新文章 | 这个按钮**只是进入预览页**！预览页**还有一个**「确认发布」按钮才是真发到服务端 | `_do_publish` 必须**两次 click**：先「预览并发布」等 3-5s，再「确认发布」 |
| 4 | `page.fill(.ProseMirror, md_text)` 把 markdown 当纯文本显示 | ProseMirror 是富文本编辑器，需要结构化节点 | `markdown.markdown(md)` 转 HTML → `ClipboardEvent('paste', {clipboardData})` 派发到 `.ProseMirror`，编辑器自动解析成 h/p/code/table 节点（见 `_paste_html_to_prosemirror`） |

### 头条号平台策略限制（不是 ToutiaoPublisher 的锅）

| 元素 | 状态 | 解决 |
|---|---|---|
| `<a>` 外链 | **paste 时被剥除** | 上游 prompts 强制用"访问 xxx.com"纯文本，**不出 markdown 链接语法** |
| `<em>` 斜体 | **被剥除** | 同上，prompts 里禁用 `*斜体*` |
| `<strong>` 加粗 | HTML 结构保留，**视觉效果不明显**（CSS reset 压平） | 接受，平台行为 |
| 标签字段 | **头条号文章没有标签** | Publisher 收 `content.tags` 但发布时忽略；可考虑接入"合集"代替 |
| 封面在文章详情页 | 不直接显示（feed/作品管理列表才显示缩略图） | 平台设计，正常 |

### 验收时的关键 SOP

**只看 `PublishResult.success=True` 和 `platform_url` 不够**。必须用 cookie 抓**作品管理后台** `https://mp.toutiao.com/profile_v4/graphic/articles` 对比 `.article-card` 数量是否真 `+1`——这才是服务端硬证据。
当前 `ToutiaoPublisher` 抓回的 `platform_url` 还停留在发布页 URL，**TODO**: 加一步去作品管理抓 `/item/{id}/` 真链接（见 §九 待办）。

## 三-B、知乎 Publisher（自建，开源缺口）

> **底层逻辑**：跟头条同款思路——gh search 关键词穷尽（zhihu publish / api / oauth / mcp / playwright）全部为空，没有可集成的成熟开源工具。方案 A（破解 `z_c0` + `x-zse-93/96` 签名）维护成本太高；方案 B（Playwright 模拟操作 + patchright 反检测）胜出。**比头条号省 4 次失败**（头条 6 次 / 知乎 2 次闭环）。

### 接入位置 + 调用契约

```
src/ai_ops/
└── publishers/
    └── zhihu.py            # ZhihuPublisher 单文件实现，含 collect_metrics（走 zhihu Web API）
```

- 注册：`publishers/registry.py` → `reg.register(Platform.ZHIHU, ZhihuPublisher, priority=10)`
- 凭证：`{"cookies": [{"name": "z_c0", ...}, {"name": "d_c0", ...}, ...]}`，Fernet 加密
- 数据采集：`collect_metrics` 直接走 `https://www.zhihu.com/api/v4/articles/{post_id}`，只需 cookie，不需要签名

### 知乎专属的 2 个工程坑

| # | 坑 | 真相 | 修法 |
|---|---|---|---|
| 1 | **`button:has-text("发布")` 误命中「发布设置」** | playwright `has-text` 是 substring 匹配，"发布设置"4 字也含"发布"，命中第一个 enabled 的是「发布设置」按钮 → 触发的是右侧发布设置面板（自动保存草稿+URL 跳 /edit） | **必须用 `:text-is("发布")` 精确文本匹配**——这条新规适用于所有用文字定位发布按钮的 Publisher |
| 2 | 发布完跳 `/p/{id}/edit` 或 `/p/{id}` 区分 | `/edit` 后缀 = 草稿；裸 `/p/{id}` = 公开页 | 抓 `PublishResult.platform_url` 时**必须看后缀**，`/edit` 视为失败要重发 |

### 知乎与其他平台对比（验收质量）

| 维度 | 知乎专栏 | 头条号 |
|---|---|---|
| 编辑器引擎 | **DraftJS** (`.public-DraftEditor-content`) | ProseMirror (`.ProseMirror`) |
| Markdown paste | `ClipboardEvent('paste')` 兼容 | `ClipboardEvent('paste')` 兼容 |
| h1/h2 分级 | **正确分级**（`#` → h1, `##` → h2） | 部分降级 |
| 代码块语法高亮 | 支持 | 不支持 |
| 表格 | 支持 | 支持 |
| **外链 `<a>`** | **保留** | **被剥除** |
| 加粗 `<strong>` | 转其他样式（原 strong 标签数 0，有损耗） | 结构保留但视觉压平 |
| 封面 | feed 列表展示 + 详情页 hero img | 仅 feed / 作品管理列表显示缩略图 |
| 发布按钮 | **单步**（一次 click 即发布） | **两步**（预览并发布 → 确认发布） |
| 标签字段 | 文章话题（手动添加，非必填）+ 投稿至问题 | **无** |

### 验收时的关键 SOP

抓取 `platform_url` 时**必须 strip `/edit` 后缀**校验：
- 命中 `/edit` → 还是草稿，状态不算"已发布"（应判 `PublishResult.success=False`，TODO 见 §九）
- 裸 `/p/{id}` → 真公开

## 三-C、微信公众号 Publisher（自建，persistent_context 路径）

> **底层逻辑**：mp 后台反爬比头条/知乎严 1 个数量级——`storage_state` cookie 模式跨进程立即失效（mp 服务端识别为新设备），即使切到 `launch_persistent_context` 整体持久化浏览器内部状态（含指纹 / IndexedDB / Service Worker cache），**playwright 仍可能被 JS 反爬识别**返回"请重新登录"。**业界标准路径 = 官方 API 或 patchright（stealth fork）**。

### 接入位置 + 调用契约

```
src/ai_ops/
└── publishers/
    └── wechat_mp.py        # WechatMpPublisher：login + health_check，publish 阶段 1 限 draft
```

- 凭证：`{"profile_dir": "/abs/path/to/wechat_mp_<account_id>", "last_login_at": "..."}`
  - 路径本身不算敏感数据，但走统一的 Fernet 加密通道，保持架构一致
  - 默认 `profile_dir = settings.data_dir / "browser_profiles" / "wechat_mp_<account_id>"`
- 浏览器：`p.chromium.launch_persistent_context(user_data_dir=profile_dir, ...)` （不是 `launch()`！）

### 三条候选路径（决策记录）

| 选项 | 工程量 | 成功率 | 适用 |
|---|---|---|---|
| A. 切换 `settings.browser_engine=patchright`（playwright stealth fork） | 30-60 min | ~70% | 个人订阅号沙盒验证 |
| **B. 官方 API**（推荐） | 1-2 天 | 100% | 服务号 + 认证 + 内容上传 API |
| C. 半自动：人工登录 + Publisher 调 API | 4-8h | 80% | 折中 |

当前 `WechatMpPublisher` 走 A（persistent_context + 当前 browser_engine）。

### 已知坑（提前固化，等回归时省时）

| # | 坑 | 应对 |
|---|---|---|
| 1 | `storage_state` 模式 cookie 跨进程失效 | 必须 `launch_persistent_context` |
| 2 | playwright 被 mp JS 反爬识别 | 切 `settings.browser_engine=patchright` 或走官方 API |
| 3 | mp 后台是 iframe 嵌套布局 | 编辑器 selector 要用 `page.frame_locator(...)` |
| 4 | 群发不可撤回 + 每天次数限制 | 阶段 1 死命只做 `upload-draft`，**不实现 send-draft** |

### 重启清单（账号到位后 4 步）

1. `POST /accounts` 创建 mp 账号 → `POST /accounts/{id}/login` 触发扫码（窗口启动 `launch_persistent_context`）
2. 如果还跑不通 → 改 `settings.browser_engine=patchright`（drop-in）+ 重跑登录
3. inspect 后台图文编辑器拿 selector（套 `ZhihuPublisher` 经验，预计含 iframe `frame_locator`）
4. 实现 `_do_publish` 仅做"存草稿"（**不实现 send-draft**，阶段 1 不群发）

## 四、二维码递交（首次登录 / cookie 失效）

`POST /accounts/{id}/login` 内部会调 `publisher.login()`，对走扫码路径的平台（zhihu / toutiao / wechat_mp）会**开窗显示扫码二维码**。当前实现是浏览器窗口直接弹出二维码（headed 模式），用户用手机 APP 扫描即可。

**已规划增量（main.py 注释已留 TODO）**：SSE 推送二维码 PNG 到 `/accounts/{id}/login/stream`，由前端展示，避免依赖 headed 窗口。在那之前，本地调试时若终端二维码糊：

```bash
# 1. 等 Publisher 写出 PNG 二维码（如使用 SAU 子进程，落在 settings.external_sau_path/cookies/...）
QR=$(ls -t cookies/{platform}_acc_{account_id}_*qrcode*.png | head -1)

# 2. sips 放大到 600x600（手机 APP 扫码更稳）
sips -Z 600 "$QR" --out /tmp/qr_big.png

# 3. 两入口同时给用户
open -a Preview /tmp/qr_big.png        # macOS 大图
# AI 对话里 Read /tmp/qr_big.png       # 多模态展示
```

`POST /accounts/{id}/login` 超时阈值 5 分钟（`asyncio.wait_for(..., timeout=300)`），超时返回 HTTP 408；**前端拿到 408 立刻发起重新登录请求**，不要等用户问。

## 五、数据落库 schema — 对齐 `PublishJob`

发布层的状态全部落在 `PublishJob` 表（`src/ai_ops/core/models.py`）。关键字段：

```python
class PublishJob(Base):
    id: int
    article_id: int                    # FK → articles.id
    account_id: int                    # FK → accounts.id
    platform: Platform                 # xiaohongshu / zhihu / toutiao / wechat_mp / ...
    status: JobStatus                  # pending / running / success / failed / retrying / dead
    publisher_kind: str                # social_auto_upload / xhs_toolkit / ...
    attempts: int                      # 已尝试次数
    max_attempts: int                  # 默认 3
    platform_post_id: str | None       # 平台侧 ID（如知乎 article_id）
    platform_url: str | None           # 发布后真实 URL
    error: str | None                  # 失败原因
    raw_response: dict                 # publisher 返回的原始 dict（含 final_url 等）
    scheduled_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
```

对应的 `Article` 侧状态机：`DRAFT → READY → SCHEDULED → PUBLISHING → PUBLISHED`（异常路径 → `FAILED` / `DEAD`）。

**重发覆盖语义**（旧版 ledger 的 `superseded_by` 字段）：
- 当前实现是新建一个 `PublishJob`（不沿用旧 job），通过 `Article.extra` 字段记录关联；后续可加 `superseded_by_job_id` 列。
- 用户 APP 端手动删旧文，是必经步骤——平台都不支持原地编辑已发布作品。

## 六、通知矩阵（事件 → 触发点 → 收件人）

**当前 ai-ops-auto 尚未实现通知模块**，下面是规划。已在 `worker.execute_job` / `health.check_all_accounts` 留好事件 hook 位，待统一接入：

| 事件 | 触发点 | 收件人 / 目的地 | 消息模板 |
|---|---|---|---|
| 单条发布成功 | `worker.execute_job` 中 `result.success=True` 分支 | 运营群（webhook） | `已发布：account_id={aid} 在 {platform} 发布《{title}》 {url}` |
| 单条发布失败 | 同上 `else` 分支 | 发布负责人（DM） | `job_id={jid} 发布失败：{error}` |
| 登录态失效 | `health.check_all_accounts` 返回 `EXPIRED` / `BANNED` | 账号负责人（DM） | `account_id={aid} 登录态失效，请 POST /accounts/{aid}/login 重登` |
| 内容污点（grep 兜底命中） | `worker.execute_job` 前置 hook（TODO） | 内容负责人（DM） | `article_id={aid} 正文含 {match}，发布已 abort，去编辑器修` |
| 一轮 fanout 完成 | scheduler 批处理 callback（TODO） | 运营群（webhook） | `article_id={aid} fanout 完成：成功 {n_ok} / 失败 {n_fail}` |

> 顶层设计：**所有发布事件都流到 IM 群里看得见**。通知模块实现见 §九 待办。

## 七、跟内容中枢的拉通

ai-ops-auto 内部数据流（前置 `Topic`/`Article` 已在 `content/manager.py`）：

```
发布前: content_mgr.transition_status(article_id, ArticleStatus.READY)
       # 内容污点 grep 兜底（TODO: 接入 worker 前置 hook）
       grep -rE "TODO|过期版本号|未替换占位符" <article.body>

发布中: worker.execute_job(job_id)
       └─ article.status = PUBLISHING（自动转移）

发布后:
  成功: PublishJob.status = SUCCESS, platform_post_id, platform_url 落库
        + Article.status = PUBLISHED
        + schedule_after_publish(job.id) → 1h / 24h / 7d 数据采集飞轮
        + （TODO）通知 webhook
  失败: PublishJob.status = RETRYING（attempts < max_attempts）或 DEAD
        + 失败联动：连续 3 次 DEAD → Account.health 升级到 BANNED
        + （TODO）通知 DM
  内容污点: article 不入 PUBLISHING，flag 在 Article.extra（TODO 接入 hook）
```

## 八、风险与对策

| 风险 | 对策 |
|---|---|
| 内容污点（错链接 / TODO / 错版本号溜出） | 发布前 grep 兜底（运营纪律，TODO 接入 worker 前置 hook） |
| 笔记发了发现内容错 | Publisher 重发 v2 + 旧 PublishJob 标记 superseded（TODO 加列）；用户去 APP 删 v1 |
| Cookie 过期 | `scheduler.health` 每天 02:00 全量 health_check，过期触发 IM 告警（TODO 接通知） |
| 二维码超时（5 分钟） | `POST /accounts/{id}/login` 用 `asyncio.wait_for(..., timeout=300)`，超时返回 408，前端立刻重发 |
| 平台改版（selector 失效） | 知乎/头条/公众号的 selector 集中在各 `*Publisher.py` 顶部常量；SAU 上游负责 xhs/抖音等 selector 维护 |
| **误发**（自动点了发布按钮） | 发布前 grep 兜底 + 失败立刻 v2 覆盖 |
| 高风控平台触发反爬 | `default_registry` 多 priority fallback：xhs 主力 `SocialAutoUploadPublisher` 挂了自动切 `XhsSkillsPublisher`（加固档）；mp 切 `settings.browser_engine=patchright` |
| 通知 webhook 调用频次过高刷屏 | 同一事件 5 分钟内去重，超阈值聚合成一条（TODO 实现在通知模块） |
| 凭证泄露 | Fernet 对称加密落库；`FERNET_KEY` 走环境变量；密钥泄漏 = 全军覆没（独立轮换流程） |

## 九、待办（参考）

### 已完成
- [x] PublisherBase 抽象 + `default_registry` 路由 + priority fallback
- [x] 小红书图文真发链路打通（27 秒，`SocialAutoUploadPublisher` 主力 + `XhsSkillsPublisher` 加固）
- [x] 头条号 `ToutiaoPublisher` 真发链路打通（4 个工程坑闭环）
- [x] 知乎 `ZhihuPublisher` 真发链路打通（2 个工程坑闭环，比头条号省 4 次失败）+ `collect_metrics` 走 Web API
- [x] `runtime/playwright_factory` 多浏览器引擎切换（playwright / chrome channel / patchright / camoufox）
- [x] 凭证 Fernet 加密落库 + 解密管线（`accounts/store.py`）
- [x] `PublishJob` 状态机 + 重试 + 失败联动 Account.health 升级
- [x] `schedule_daily_health_check` 02:00 全量探活
- [x] 发布成功后 `schedule_after_publish` 1h/24h/7d 数据采集飞轮

### TODO
- [ ] `WechatMpPublisher._do_publish` 实现（阶段 1 仅 `upload-draft`，**不实现 send-draft**），按 §三-C 重启清单走
- [ ] `worker.execute_job` 前置 grep 兜底 hook（防错链接 / TODO / 过期版本号溜出）
- [ ] 通知模块（webhook + DM）：事件 hook 接入 + 同事件 5 分钟去重聚合
- [ ] `POST /accounts/{id}/login/stream` SSE 推送二维码 PNG（前端展示，去 headed 依赖）
- [ ] `ToutiaoPublisher` 抓取真实 `platform_url`（当前停在发布页 URL，需要去作品管理抓 `/item/{id}/`）
- [ ] `ZhihuPublisher` 抓取 `platform_url` 时 strip `/edit` 后缀判草稿/公开
- [ ] `PublishJob` 加 `superseded_by_job_id` 列，实现重发覆盖追踪
- [ ] 多平台横向扩展：百家号 / 搜狐号（套用 §三-A / §三-B 的工程坑清单 + 验收 SOP）
- [ ] 小红书"编辑已发布笔记"能力（开源缺口，预计自建 `XhsEditPublisher` 复用 cookie）
