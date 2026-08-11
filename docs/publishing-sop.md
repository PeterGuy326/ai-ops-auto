# Publishing SOP（ai-ops-auto 发布层）

> **文档性质**：这是历史工程笔记和适配器 SOP，不是当前支持承诺。平台成熟度、
> 最后验证日期和 metrics 现状以 [平台能力矩阵](platform-capabilities.md) 为唯一信源。
>
> **发布层抓手 = `PublisherBase` 抽象 + 多平台子类（`ZhihuCliPublisher` / `YoutubeUploaderPublisher` / `ZhihuPublisher` / `ToutiaoPublisher` / `WechatMpPublisher` / `SocialAutoUploadPublisher` / `XhsSkillsPublisher` / `GitHubPagesPublisher`）**。
> 浏览器自动化委托给 Playwright 兼容引擎，不重写底层；fallback 只表示技术适配路径，
> 不保证绕过检测、平台审核或账号限制。
>
> 历史记录：2026-05-17 曾记录 xhs 单篇图文真发。该记录本次未复测，不应当作当前耗时或成功率承诺。

## 一、为什么走 Publisher + CLI/API/浏览器 Adapter，不重写 Chrome MCP

| 维度 | 自研 Chrome MCP（废弃） | Publisher (Playwright / patchright) |
|---|---|---|
| 浏览器栈 | 控用户主 Chrome | 独立 Chromium / Chrome channel / 可选兼容引擎（按 `settings.browser_engine` 切换） |
| 文件上传 | cliclick + NSOpenPanel + keystroke 路径（**8 轮死磕未通**） | `page.set_input_files(path)` 一行调用 |
| 焦点 | 必须 activate 抢前台 | **完全后台**（headless 或 headed 都不抢焦点） |
| 跨平台 | 只 macOS | macOS / Linux / Windows |
| 平台兼容维护 | selectors.yaml 每周修 | 浏览器引擎 + Publisher selector；平台变化仍需回归验证 |
| 多实现 fallback | 没有 | `default_registry` 按 priority 路由；只在确认写操作未开始时 fallback |

> **底层逻辑**：优先复用可审计、可锁版本、能返回机器回执的官方 API/CLI；没有合格 CLI
> contract 时才保留薄浏览器 Adapter。第三方工具的存在不等于稳定能力。

## 二、统一发布流程

```
[1] 上游编排：Article 状态机推进到 READY / SCHEDULED
    POST /articles {topic_id, title, body, content_type, target_account_ids}
        ↓
[2] API/分发层把 (article, account) 笛卡尔积成 PublishJob 落库
    （也可手动 POST /jobs/{id}/run 触发）
        ↓
[3] 发布前 grep 兜底（污点教训）
    grep -rE "TODO|过期版本号|未替换占位符" <article.body>  → 任一命中即 fail-fast
    （worker 的发布前检查已经实现；人工审核仍不能省略）
        ↓
[4] 独立 `ai-ops worker` 扫描到期 job（需 AUTO_PUBLISH_ENABLED=true）并调用 execute_job
    ├─ check_rate_limit（养号期 + 间隔 + 单日上限）
    ├─ get_credential（Fernet 解密 Account.encrypted_credential）
    ├─ default_registry.resolve(platform) → 拿到按优先级排序的 Publisher 列表
    └─ 依次 publisher.publish(account_id, credential, content)
       success=True、effect_applied=True 或 outcome_uncertain=True 任一成立即停止
       只有明确失败、无副作用且结果确定的预检失败才允许切换实现
        ↓
[5] Publisher 内部：调用版本化 CLI/API，或打开浏览器完成平台操作
    （登录、超时、结果确认和敏感输出边界由具体 Adapter 负责）
        ↓
[6] 回写 PublishJob：status=SUCCESS, platform_post_id, platform_url
    Article.status 按所有未被替代的 fan-out job 聚合；全部成功后才是 PUBLISHED
    若有可验证 post identity，再排程后续数据采集
```

> **当前安全语义**：审核是控制面状态流，不是强制 human gate；审批、分发和执行端点共用一个
> 管理 `API_KEY`，项目自身不能证明审批者是人。`AUTO_PUBLISH_ENABLED=false` 只关掉后台扫描，
> 持有管理 key 的调用方仍可显式执行 `/jobs/{id}/run`。grep 只是辅助检查；要强制人工授权，
> 需在外部网关/工作流隔离 Agent 与管理 key。“失败立刻重发覆盖”可能造成重复公开内容。

## 三、外部依赖装机 + Publisher 调用契约

### 一次性装机（每台新机器）

```bash
# 1. ai-ops-auto 本身
cd /path/to/ai-ops-auto
uv venv --python 3.12 && . .venv/bin/activate
uv pip install -e .

# 2. 浏览器引擎（默认 playwright_chrome_channel = 复用本机真 Chrome）
PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright" playwright install chromium

# 3. 可选 Playwright 兼容引擎（必须单独验证版本与平台条款）
uv pip install patchright
PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright" patchright install chromium

# 4. social-auto-upload 上游（锁定本轮审计 commit；升级前重新审计许可证与契约）
git init external/social-auto-upload
git -C external/social-auto-upload remote add origin https://github.com/dreammis/social-auto-upload.git
git -C external/social-auto-upload fetch --depth 1 origin 008e4ff66abdf48eb1f4b999272ef979711af436
git -C external/social-auto-upload checkout --detach FETCH_HEAD
# settings.external_sau_path 指向该目录，由 SocialAutoUploadPublisher 子进程调用
# EXTERNAL_SAU_URL 仅限 loopback/可信私网：当前集成本身无认证，禁止直接暴露公网

# 5. 凭证加密密钥
export FERNET_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# 6. 可选：知乎 CLI canary（第三方、精确版本、独立 tool 环境）
uv tool install 'pyzhihu-cli==0.2.4'
# .env: ZHIHU_CLI_ENABLED=true
# ai-ops zhihu-login <account_id>

# 7. 可选：从上游 release 安装并核对 youtubeuploader v1.25.5
# 上游没有 auth-only 命令。在可信终端为每个 account_id 人工预置
# client_secrets.json/request.token；首次授权可能与 private canary 上传绑定，
# 不要在 worker 中触发首次 OAuth。
# .env: YOUTUBE_UPLOADER_ENABLED=true
```

切换浏览器引擎：改 `settings.browser_engine`，可选值：
- `playwright_chromium`：上游 Chromium，最干净
- `playwright_chrome_channel`：使用本机 Chrome channel（默认）
- `patchright`：Playwright 兼容 fork；平台效果与条款需由部署者验证
- `camoufox`：Firefox 衍生引擎，API 不兼容 Playwright，需独立适配和验证

### Publisher 调用契约（所有平台共用 `PublisherBase`）

所有 Publisher 子类实现这 4 个方法：

| 方法 | 用途 | 调用入口 |
|---|---|---|
| `login(account_id, credential)` | 平台相关的登录/既有状态验证；有些适配器更新 DB credential dict，有些只使用磁盘 profile | `POST /accounts/{id}/login`；显式终端登录可能另有 CLI |
| `publish(account_id, credential, content)` | 单次发布，`content` 是平台无关的 `PublishContent` | `POST /jobs/{id}/run` → `worker.execute_job` |
| `health_check(account_id, credential)` | 登录态 / 风控感知，返回 `AccountHealth` | `scheduler.health.check_all_accounts`（默认每天 02:00） |
| `collect_metrics(post_id, post_url, credential)` | 数据采集（显式 `supports_metrics=True` 后 override；默认不支持并跳过，绝不伪造 0） | `scheduler.metrics.collect_one` / `POST /jobs/{id}/collect` |

调用方都通过 `default_registry.resolve(Platform.XXX)` 拿 Publisher 列表（按 priority 排序）。只有
`success=False`、`effect_applied=False` 且 `outcome_uncertain=False` 的确定无副作用失败才会 fallback；
其余三种停止条件分别是成功、已发生副作用或结果未知。

### 支持的平台 / Publisher 对应表

| Platform | 主力 Publisher（priority） | 加固 / fallback Publisher | 备注 |
|---|---|---|---|
| `XIAOHONGSHU` | `SocialAutoUploadPublisher`（10） | `XhsSkillsPublisher`（20） | 均无 post ID；只有前者未启动时才可 fallback，启动后需人工对账 |
| `DOUYIN` / `BILIBILI` / `KUAISHOU` | `SocialAutoUploadPublisher`（10） | — | 走已审计 SAU CLI 命令；无 post ID 不落 SUCCESS |
| `WECHAT_VIDEO` | `SocialAutoUploadPublisher`（10） | — | 仅 SAU HTTP `/postVideo`；当前集成无认证、无 post ID |
| `YOUTUBE` | `YoutubeUploaderPublisher`（5，flag 默认关闭） | — | v1.25.5 receipt canary；当前没有 SAU fallback |
| `TIKTOK` | — | — | 当前没有注册 Publisher；优先规划官方 Content Posting API |
| `ZHIHU` | `ZhihuCliPublisher`（5，flag 默认关闭） | `ZhihuPublisher`（10） | CLI 0.2.4 canary；只有 preflight 安全失败才回退浏览器 |
| `TOUTIAO` | `ToutiaoPublisher`（10） | — | 开源缺口，自建 |
| `WECHAT_MP` | `WechatMpPublisher`（10） | — | 开源缺口，persistent_context 路径 |
| `GITHUB_PAGES` | `GitHubPagesPublisher`（10） | — | dry-run 默认开启；受控图片根目录 + 仓库锁 + commit 路径/SHA 对账 |

## 三-A、头条号 Publisher（自建，开源缺口）

> **历史决策**：当时使用的 social-auto-upload 版本没有头条上传器，因此仓库增加了
> `PublisherBase` 浏览器适配器。该选择不代表当前平台可用性，见能力矩阵。

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

> 历史调试记录归纳了以下 selector/流程问题；它们可能随平台 UI 改版而失效。

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

**只看提交按钮完成不够**。当前 `ToutiaoPublisher` 会到作品管理后台
`https://mp.toutiao.com/profile_v4/graphic/articles` 读取严格匹配的 `/item/{id}/` 公网链接；只有拿到
数字 post ID 与规范公开 URL 才返回成功。抓不到时进入 `outcome_uncertain`、停止自动重试，并要求人工
核对作品列表。专用账号 canary 仍需额外确认作品列表数量和公网可见性，mock 契约不是平台证据。

## 三-B、知乎 Publisher（CLI canary + 浏览器 fallback）

> **当前决策**：已接入第三方 `pyzhihu-cli==0.2.4` 的受控 canary，但默认关闭；它使用的
> 仍是非官方消费端接口。历史浏览器调试次数和 CLI 源码测试都不构成当前真发证据。

### 接入位置 + 调用契约

```
src/ai_ops/
└── publishers/
    ├── zhihu_cli.py        # 外部 CLI 薄适配、版本/隔离/结果不确定语义
    └── zhihu.py            # Playwright fallback + collect_metrics
```

- 注册：`ZHIHU_CLI_ENABLED=true` 时 CLI priority=5；Playwright priority=10
- CLI 登录：`ai-ops zhihu-login <account_id>`，每个账号使用独立 HOME；这是 CLI 路径唯一会
  显示二维码的入口，禁止 cookie argv。登录态留在该 HOME 的磁盘文件中，不进入 Fernet 数据库密文。
- API 登录：启用 CLI canary 时，`POST /accounts/{id}/login` 只验证上述 HOME 是否已经登录；
  未登录会提示执行终端命令，不弹二维码，也不会自动改走 Playwright 登录。
- 浏览器 fallback 凭证：`{"cookies": [{"name": "z_c0", ...}, ...]}`，作为 DB credential blob
  由 Fernet 加密；它与 CLI 的独立 HOME 是两条不同登录路径。
- 数据采集：`collect_metrics` 直接走 `https://www.zhihu.com/api/v4/articles/{post_id}`，只需 cookie，不需要签名

### CLI 0.2.4 canary 边界

- 仅接 `article`；视频/音频拒绝，topic 只读 `extra.zhihu_topic_ids` 的数字 ID。
- 只允许受控 `ZHIHU_CLI_ASSET_ROOT` 内、非 symlink、经验证的 JPEG；正文设 60 KB 上限。
- 上游没有 write `--json`、stdin/`--content-file`、`--config-dir` 或 idempotency key。
- 只有 `rc=0 + Article published marker + /p/<同一数字ID>` 才算 SUCCESS。
- `article` 子进程启动后发生 rc 非 0、超时、rc=0 无 ID 或输出损坏，一律
  `outcome_uncertain=True`，job 进入需人工核验的 FAILED，不 fallback、不自动 retry。
- Markdown 会先转 HTML，但上游仍额外套 `<p>`，正文也会出现在本机进程 argv；长文和复杂格式
  必须在专用账号 canary。正式主链路要等待上游补 `--content-file --format html --json --config-dir`。

### 知乎专属的 2 个工程坑

| # | 坑 | 真相 | 修法 |
|---|---|---|---|
| 1 | **`button:has-text("发布")` 误命中「发布设置」** | playwright `has-text` 是 substring 匹配，"发布设置"4 字也含"发布"，命中第一个 enabled 的是「发布设置」按钮 → 触发的是右侧发布设置面板（自动保存草稿+URL 跳 /edit） | **必须用 `:text-is("发布")` 精确文本匹配**——这条新规适用于所有用文字定位发布按钮的 Publisher |
| 2 | 发布完跳 `/p/{id}/edit` 或 `/p/{id}` 区分 | `/edit` 后缀 = 草稿；裸 `/p/{id}` = 公开页 | 抓 `PublishResult.platform_url` 时**必须看后缀**；`/edit` 转人工核验，不能自动重发 |

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
- 命中 `/edit` → 还是草稿，状态不算"已发布"（应进入人工核验且禁止自动重发；TODO 见 §九）
- 裸 `/p/{id}` → 真公开

## 三-C、YouTube Publisher（官方 API CLI canary）

> **当前决策**：已接入 `youtubeuploader v1.25.5`，但默认关闭且未真发。
> 只有专用频道 private canary 和平台端可见验证都通过后才可以扩大。

- 注册：`YOUTUBE_UPLOADER_ENABLED=true` 时 CLI priority=5；SAU priority=10。
- 凭证：`YOUTUBE_UPLOADER_PROFILE_ROOT/account_<id>/` 目录权限 `0700`，
  `client_secrets.json` 和 `request.token` 权限 `0600`。
- 输入：只允许受控 `YOUTUBE_UPLOADER_ASSET_ROOT` 内的单个本地视频；元数据
  写临时 JSON，标题/正文/token 内容不进 argv 或 `raw_response`；`request.token` 的文件路径会作为
  `-cache=...` 参数传给子进程。
- 授权：上游没有 auth-only 命令。`POST /accounts/{id}/login` 只验证预置文件；首次 OAuth 必须由人
  在可信终端完成，并将可能与授权绑定的第一次 private canary 当作真实写操作审核。
- 回执：只认 `-metaJSONout` 中的合法 video ID。ID 与请求 privacy 一致时确认成功，即使进程随后
  非零或超时，也保留 `published_partial/reconcile` 证据且绝不重传；privacy 不匹配时保留 ID/URL，
  返回失败并标记 `effect_applied=True`，转人工对账。
- 无合法回执：只要写进程已启动就进入 `outcome_uncertain`，不 fallback、不自动 retry；确认
  未启动的预检失败会作为确定失败返回。当前没有第二个 YouTube Publisher 可供 fallback。

2020-07-28 后创建且未经审核的 API 项目，通过 `videos.insert` 上传的
视频会被限制为私有；不得把本地请求了 `public` 当成实际公开证据。
见 [YouTube Data API `videos.insert`](https://developers.google.com/youtube/v3/docs/videos/insert)。

## 三-D、微信公众号 Publisher（自建，persistent_context 路径）

> **历史决策**：曾观察到 `storage_state` 跨进程登录态不稳定，因此实验性路径使用
> `launch_persistent_context`。优先采用平台官方 API；浏览器路径仍可能失效，且当前为 Stub。

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

| 选项 | 证据状态 | 适用 |
|---|---|---|
| A. 切换 `settings.browser_engine=patchright` | 未建立当前真平台成功率 | 仅在条款允许的测试账号 canary |
| **B. 官方 API**（推荐） | 需按官方资质、配额和审核验证 | 服务号 + 认证 + 内容上传 API |
| C. 半自动：人工登录 + Publisher 调 API | 未建立当前真平台成功率 | 需保留人工最终确认的场景 |

当前 `WechatMpPublisher` 走 A（persistent_context + 当前 browser_engine）。

### 已知坑（提前固化，等回归时省时）

| # | 坑 | 应对 |
|---|---|---|
| 1 | `storage_state` 模式 cookie 跨进程失效 | 必须 `launch_persistent_context` |
| 2 | 浏览器路径被平台拒绝或要求重登 | 优先走官方 API；否则停用并重新验证适配器 |
| 3 | mp 后台是 iframe 嵌套布局 | 编辑器 selector 要用 `page.frame_locator(...)` |
| 4 | 群发不可撤回 + 每天次数限制 | 阶段 1 死命只做 `upload-draft`，**不实现 send-draft** |

### 重启清单（账号到位后 4 步）

1. `POST /accounts` 创建 mp 账号 → `POST /accounts/{id}/login` 触发扫码（窗口启动 `launch_persistent_context`）
2. 如果还跑不通 → 停止操作，核对平台条款和能力矩阵，再决定是否在测试账号验证其他引擎
3. inspect 后台图文编辑器拿 selector（套 `ZhihuPublisher` 经验，预计含 iframe `frame_locator`）
4. 实现 `_do_publish` 仅做"存草稿"（**不实现 send-draft**，阶段 1 不群发）

## 四、二维码递交（首次登录 / cookie 失效）

`POST /accounts/{id}/login` 只调用当前优先级最高的 `publisher.login()`；具体行为由该 Adapter 决定。
支持 headed 浏览器扫码的 Adapter 可能开窗，但不能把它概括成所有平台的统一行为。尤其是启用
知乎 CLI canary 时，该 API 只验证独立 HOME 中的现有登录态；二维码必须在可信终端显式运行
`ai-ops zhihu-login <account_id>`。YouTube 路径也只验证预置 OAuth 文件，不会在 API/worker 中授权。

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

`POST /accounts/{id}/login` 的 Adapter 调用超时阈值为 5 分钟
（`asyncio.wait_for(..., timeout=300)`），超时返回 HTTP 408。对实际会启动浏览器/扫码子进程的
Adapter，先确认上一个进程已经退出，再由用户显式决定是否重试，避免并发登录流程。

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

**重发覆盖语义**：
- 当前实现会新建一个 `PublishJob`（不沿用旧 job），并用旧 job 的
  `superseded_by_job_id` 指向替代任务。
- 该字段只记录控制面关系，不代表平台端旧内容已删除。是否编辑、删除或重新发布必须由人先核对
  平台真实状态，不能假设所有平台都具有相同能力。
- 旧 job 如果记录了 `outcome_uncertain` 或 `effect_applied`，重发请求必须在
  人工核对平台后显式传 `POST /jobs/{id}/republish?platform_checked=true`。
  该参数只是持有管理 key 的调用方声明“已核对”，系统没有审批者身份、签名或二次授权证据；
  不得把它当作强制 human gate。

## 六、通知矩阵（事件 → 触发点 → 收件人）

当前已有 webhook/lark-cli 通知适配器，但两者都发送到部署级全局目标
（`FEISHU_WEBHOOK_URL` / `LARK_CLI_CHAT_IDS`），不会根据 Account owner 自动私聊。平台权限与
实际投递仍取决于部署配置。下表明确区分已接线事件和规划项：

| 事件 | 状态 | 触发点 | 当前目的地 | 消息模板 |
|---|---|---|---|---|
| 单条发布成功 | 已接线 | `worker.execute_job` 中成功分支 | 全局 webhook / lark-cli 目标 | `已发布：account_id={aid} 在 {platform} 发布《{title}》 {url}` |
| 单条发布失败 | 已接线 | `worker.execute_job` 中失败分支 | 全局 webhook / lark-cli 目标 | `job_id={jid} 发布失败：{error}` |
| 登录态失效 | 已接线 | `health.check_all_accounts` 返回需告警状态 | 全局 webhook / lark-cli 目标 | `account_id={aid} 登录态失效，请处理登录` |
| 内容污点（grep 兜底命中） | 未接线 | 发布前检查 | 规划中的全局目标 | `article_id={aid} 正文含 {match}，发布已 abort` |
| 一轮 fanout 完成 | TODO | 批处理 callback | 规划中的全局目标 | `article_id={aid} fanout 完成：成功 {n_ok} / 失败 {n_fail}` |

> 产品目标是关键发布事件可见；当前并非所有事件都已接线，也没有按负责人 DM 的路由模型。

## 七、跟内容中枢的拉通

ai-ops-auto 内部数据流（前置 `Topic`/`Article` 已在 `content/manager.py`）：

```
发布前: content_mgr.transition_status(article_id, ArticleStatus.READY)
       # worker 在平台调用前执行内容污点检查

发布中: worker.execute_job(job_id)
       └─ article.status = PUBLISHING（自动转移）

发布后:
  成功: PublishJob.status = SUCCESS, platform_post_id, platform_url 落库
        + Article.status 按 fan-out job 聚合（全部成功后才是 PUBLISHED）
        + schedule_after_publish(job.id) → 1h / 24h / 7d 数据采集飞轮
        + 尝试发送已配置的通知
  失败: PublishJob.status = RETRYING（attempts < max_attempts）或 DEAD
        + 失败联动：24h 内 3 次 DEAD → Account.health 升级到 BANNED，暂停 7 天
        + 尝试发送已配置的通知
  内容污点: 当前 job 进入非重试失败路径，不调用 Publisher
```

## 八、风险与对策

| 风险 | 对策 |
|---|---|
| 内容污点（错链接 / TODO / 错版本号溜出） | worker 发布前检查 + 人工审核；关键词检查不等于完整内容安全系统 |
| 笔记发了发现内容错 | 先停 worker 并确认平台端状态；由人决定编辑/删除/重发，并在 PublishJob 留完整审计 |
| Cookie 过期 | `scheduler.health` 每天 02:00 全量 health_check；通知仅在 adapter/目标配置有效时投递 |
| BANNED 永久锁死 | 7 天内 worker 始终禁止发布；到期后每日只读探活，仅明确 `HEALTHY` 恢复，`UNKNOWN` 或任何不健康结果继续保持 BANNED。API 的 `health_recheck_at` 只表示可探测时间，不表示可发布 |

publish 与每日 health check 通过 `DATA_DIR/locks/accounts` 下的内核文件锁串行访问同一账号
profile；health 不等待繁忙账号，publish 有界等待。探活写回前还会再次检查 RUNNING job、凭证和
健康版本，避免陈旧结果覆盖。显式 login 与 metrics 尚未加入这把 lease，同账号发布期间不要登录。
| 二维码超时（5 分钟） | 登录返回 408；确认旧进程退出后，由用户显式决定是否重试 |
| 平台改版（selector 失效） | 知乎/头条/公众号的 selector 集中在各 `*Publisher.py` 顶部常量；SAU 上游负责 xhs/抖音等 selector 维护 |
| **误发**（自动点了发布按钮） | `AUTO_PUBLISH_ENABLED=false` 仅关闭后台扫描；审核状态流 + 外部 human gate + 单平台 canary。事故后停 worker、撤销管理端访问并人工处置 |
| 平台拒绝自动化或页面变化 | 停止自动发布并人工核对；fallback 只能处理适配器故障，不能绕过平台限制 |
| 通知 webhook 调用频次过高刷屏 | 使用现有滑窗去重；阈值和真实投递行为需在部署环境验证 |
| 凭证泄露 | DB credential blob 用 Fernet；`FERNET_KEY` 走密钥管理。外部 CLI HOME/OAuth/cookie/浏览器 profile 不在该加密边界内，另用文件权限和主机隔离；任一密钥泄漏都需独立轮换 |
| CLI 退出 0 但无 post ID | 不作为成功证据；标记 `outcome_uncertain`，停止 fallback/retry，人工对账 |

## 九、待办（参考）

### 历史实现记录（不等于当前平台承诺）
- [x] PublisherBase 抽象 + `default_registry` 路由 + priority fallback
- [x] 小红书图文真发有 2026-05-17 历史记录（本次未复测）
- [x] 头条号 `ToutiaoPublisher` 真发链路打通（4 个工程坑闭环）
- [x] 知乎 `ZhihuPublisher` 真发链路打通（2 个工程坑闭环，比头条号省 4 次失败）+ `collect_metrics` 走 Web API
- [x] `runtime/playwright_factory` 多浏览器引擎切换（playwright / chrome channel / patchright / camoufox）
- [x] 凭证 Fernet 加密落库 + 解密管线（`accounts/store.py`）
- [x] `PublishJob` 状态机 + 重试 + 失败联动 Account.health 升级
- [x] `schedule_daily_health_check` 02:00 全量探活
- [x] 发布后指标排程代码路径（只在 Publisher 有可验证 post identity/metrics 时构成真闭环）

### TODO
- [x] `WechatMpPublisher._do_publish` 草稿保存路径（**不实现 send-draft**）；当前只有 mock 契约，
  selector/草稿后台回执仍需专用账号 canary，不能称为正式发布
- [x] `worker.execute_job` 发布前污点词兜底（仍需扩展为可配置规则）
- [x] webhook/lark-cli 通知基础适配与去重（真实投递需部署端验证）
- [ ] `POST /accounts/{id}/login/stream` SSE 推送二维码 PNG（前端展示，去 headed 依赖）
- [x] `ToutiaoPublisher` 仅在作品管理后台返回严格 `/item/{id}/` 时确认成功；未确认结果转人工对账
- [x] `ZhihuPublisher` 严格解析 `/p/{id}` 并拒绝 `/edit` 草稿/异常 URL
- [x] `PublishJob.superseded_by_job_id` 与手动重发覆盖追踪
- [ ] 多平台横向扩展：百家号 / 搜狐号（套用 §三-A / §三-B 的工程坑清单 + 验收 SOP）
- [ ] 小红书"编辑已发布笔记"能力（开源缺口，预计自建 `XhsEditPublisher` 复用 cookie）
