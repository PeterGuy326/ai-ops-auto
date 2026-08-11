# CLI Adapter 选型与迁移

> 最近审计：2026-08-11。结论来自源码、包元数据和官方 API 文档，没有使用真实账号执行写操作。
> “CLI 可调用”不等于“可作为发布成功依据”；最终成熟度仍以[平台能力矩阵](platform-capabilities.md)为准。

## 决策原则

CLI 是 Publisher 的一种执行后端，不是新的控制面。一个发布 CLI 只有同时满足以下条件才可进入
主链路：

1. 来源、许可证、固定版本和供应链可审计。
2. 登录与写操作分离；凭证不进入 argv、日志或任务回执。
3. 能返回机器可读的 `post_id`/URL，或能做可靠的 read-after-write。
4. 超时、取消、进程回收和输出上限明确。
5. “可能已写入但未确认”能表达为 `outcome_uncertain`，并阻断 fallback/retry。

本项目期望的归一化回执是：

```json
{
  "ok": true,
  "state": "published",
  "platform": "example",
  "post_id": "123",
  "url": "https://example.test/posts/123",
  "adapter_version": "1.2.3",
  "verified_at": "2026-08-10T00:00:00Z"
}
```

没有 post identity 的写操作最多是 `unknown`，不能落 `SUCCESS`。

## 平台矩阵

| 平台 | 候选 | 回执与边界 | 决策 |
|---|---|---|---|
| 知乎 | [`pyzhihu-cli 0.2.4`](https://pypi.org/project/pyzhihu-cli/) | 社区 Alpha，并非知乎官方 CLI；文章/想法/提问写命令无稳定 JSON 回执 | 已接 feature-gated canary；严格解析 ID，unknown 禁止 fallback/retry |
| 小红书 | [`xhs-cli 0.1.4`](https://pypi.org/project/xhs-cli/) | 图文有 `--json`，但 stdout 混 Rich 进度；无视频/定时 | 后续可选图文 canary，先补纯 JSON contract |
| 小红书 | [`redbook-cli 0.2.0`](https://pypi.org/project/redbook-cli/) | 图文/视频/定时，但没有结构化发布 ID，并会下载预编译二进制 | 不进主链路 |
| 小红书 | [`flow-xhs 0.1.2`](https://pypi.org/project/flow-xhs/) | JSON/JSONL 较完整，但没有可审计源码和许可证 | 拒绝默认集成 |
| 小红书 | [`xiaohongshu-mcp`](https://github.com/xpzouying/xiaohongshu-mcp) | 活跃但仍是本机 Chrome/Rod 自动化，不是官方接口 | 只允许 localhost、版本锁定、tool allowlist 和严格 readback 的可选插件 |
| 抖音/头条视频 | [官方开放平台](https://open.douyin.com/platform/resource/docs/openapi/video-management/douyin/create/create-video/) | OAuth/scope/审核后可返回 item id；用户必须明确知情 | 后续官方 API 插件优先；`dy-cli` 社区逆向/浏览器路径不默认接入 |
| 抖音/快手/小红书/B 站/视频号 | [`social-auto-upload@008e4ff`](https://github.com/dreammis/social-auto-upload/commit/008e4ff66abdf48eb1f4b999272ef979711af436) | 当前 main 已标 MIT，但旧审计 pin 仍缺 post ID/URL；B 站路径还可能运行时下载/更新 biliup | 保留兼容 fallback；禁止跟随 main 和运行时自更新，先补结构化回执 |
| 快手 | [官方 OpenAPI](https://open.kuaishou.com/platform/openApi) | OAuth + `user_video_publish` 等权限，可形成 upload/publish/photoId/readback 链路 | 官方 API 薄插件优先；`ks-miniprogram-ci` 只是小程序构建上传，不适用 |
| B 站 | [官方 Open Platform](https://openhome.bilibili.com/doc) | 提供视频稿件/专栏发布、删除、查询、沙箱和 webhook，需主体认证及 UP 授权 | 路线改为官方 API 第一；`biliup` 只作 opt-in fallback |
| B 站 | [`biliup 1.2.2`](https://pypi.org/project/biliup/) | 社区上传器；底层能得到 aid/bvid，但 CLI 回执仍不够稳定 | 不再作为首选；只在固定版本和 JSON/readback shim 后 canary |
| B 站 | [`bilibili-cli 0.6.2`](https://pypi.org/project/bilibili-cli/) | 读取/互动为主，只发文字动态，不上传视频 | 可选读取 Adapter |
| 微信公众号 | [官方草稿/发布 API](https://developers.weixin.qq.com/doc/offiaccount/Publish/Publish.html) | 草稿 ID、publish ID 和状态轮询边界明确，但账号类型/认证资格受限 | 自建官方 API 插件优先；先验证账号当前权限 |
| 微信公众号 | [`ghostwriter-cli 0.1.1`](https://pypi.org/project/ghostwriter-cli/) | 基于官方 API 只写草稿箱，不执行最终群发 | 可选 `draft` Adapter；不能冒充 `published` |
| 百家号/搜狐号 | 无已核验的可信独立发布 CLI | 百家号需先在账号后台确认当前官方合同/资格；搜狐号有条件账号可用内容同步权益 | 保留 default-off Stub；有官方同步/API 权益时优先，不猜测私有接口 |
| TikTok | [官方 Content Posting API](https://developers.tiktok.com/products/content-posting-api) | 有 OAuth、publish ID 和状态查询，但不是现成 CLI | 未来用官方 API 做薄 CLI/Publisher |
| TikTok | [`tiktok-uploader 1.2.0`](https://pypi.org/project/tiktok-uploader/) | Playwright CLI，无 post ID/URL/JSON，失败退出语义不可靠 | 仅浏览器 fallback 候选 |
| YouTube | [`youtubeuploader v1.25.5`](https://github.com/porjo/youtubeuploader/releases/tag/v1.25.5) | 基于官方 Data API；`-metaJSONout` 可得到 video resource/ID | 已接 default-off canary；receipt 是成功边界，当前没有 SAU fallback |
| GitHub Pages | 当前：`git` + 固定 `pnpm|npx`；候选：[`gh v2.97.0`](https://github.com/cli/cli/releases/tag/v2.97.0) | 当前只能把本地 commit 与 source branch remote SHA 对账；`gh api`/Actions 可作为后续部署状态来源 | 已加固现有 push；`gh` 尚未集成，Pages deploy/live URL readback 待补 |

YouTube 还有平台级前置条件：2020-07-28 后创建且未经审核的 API 项目，通过
`videos.insert` 上传的视频会被限制为私有，需要完成 Google 审核才能公开。见
[YouTube Data API `videos.insert`](https://developers.google.com/youtube/v3/docs/videos/insert)。
`youtubeuploader` 上游没有只做授权、不上传的 auth-only 命令；首次 OAuth 必须由运维人员在可信
终端预置，必要时把第一次 private canary 视为可能产生真实视频的人工审核操作。

跨平台聚合候选 `huimei` 虽声称覆盖多平台并提供 JSON，但默认模式会连接第三方云服务创建任务，
且包中没有相应平台 uploader；不作为可独立审计的本地发布后端。

## 迁移次序

### Wave 0：控制面语义（本轮）

- `PublishResult.outcome_uncertain` 成为一等字段。
- unknown 写结果停止 Publisher fallback 和 durable retry。
- CLI 子进程统一要求版本门禁、argv list、最小环境、超时/取消回收、脱敏回执。
- SAU/XhsSkills 这类无 post identity 的旧 CLI 不再把 `exit 0` 伪装成 SUCCESS；
  进程启动后一律 unknown，且不再把正文/命令输出落入任务回执。
- 知乎 0.2.4 作为默认关闭的 canary，Playwright 保持 rollback 路径。

上述停止语义已经覆盖迁移后的 CLI/Git Adapter。旧 Playwright Adapter 仍是 Experimental；
它们没有全部完成相同的“写入是否开始”建模，不能仅凭本节推断异常后一定能安全 fallback。

### Wave 1：有 post identity 的路径

1. **已落地**：GitHub Pages 只允许固定 `pnpm|npx`/git argv，用本地 commit SHA
   与 source branch 的 `git ls-remote` 对账。live 流程持有仓库级跨进程锁，commit 路径
   必须与本任务文章/图片精确一致。这只证明远端分支接受了 commit；下一步才是集成 `gh`
   或等价 API，补 Actions/Pages deploy 与 live URL readback。
2. **已落地**：`youtubeuploader` 每账号隔离 OAuth 文件，固定 v1.25.5，解析
   `-metaJSONout` video ID；默认关闭，等专用频道 private canary。
3. **待实现**：B 站改接官方 Open Platform；`biliup` JSON shim 只保留为可选 fallback，
   不再作为主路线。
4. **待实现**：优先按 Publisher Plugin SDK 接官方抖音/头条视频、快手、微信公众号和
   TikTok API。插件进入 registry 前仍要完成凭据权限、回执轮询和专用账号 canary。

### Wave 2：有限场景 Adapter

- `xhs-cli` 或本机 `xiaohongshu-mcp` 只作为默认关闭的图文 canary，先补严格 readback。
- `ghostwriter-cli` 只建公众号草稿，状态命名必须是 `draft`。
- TikTok 优先官方 Content Posting API，不追逐无 ID 的浏览器 CLI。

### 保留 fallback

视频号和当前小红书视频继续由 SAU/浏览器路径承接；抖音/快手则逐步迁到官方 API。兼容路径只有补齐版本锁定、
结构化 post identity 和真实 canary 后，才有资格从 fallback 升到主链路。

## 上游协作清单

知乎 CLI 的正式转型依赖以下 agent contract：

- write 命令 `--json`，无 ID 必须非零或返回 `state=unknown`；
- `--content-file`/stdin + `--format html`，避免正文进 argv 和非法 `<p>` 嵌套；
- `--config-dir`，不再依赖伪造 HOME；
- 发布后本人文章 readback 和可选 idempotency key。

B 站主路线应以官方 Open Platform 的稿件 ID/查询/webhook 为准；若继续使用 `biliup` fallback，
仍需把 aid/bvid 原样暴露到 CLI JSON。SAU 当前 main 虽已出现 MIT 标识，生产仍必须固定审计版本、
禁用运行时下载/更新，并补统一发布回执。

## 已接入 CLI 的运行边界

- **知乎**：只有精确成功 marker 与数字文章 URL 一致才确认；账号独立
  HOME，二维码登录只能通过显式终端命令 `ai-ops zhihu-login <account_id>` 发生。
  登录与 worker 发布共用账号操作锁，避免同时读写同一 profile。在线验证成功后命令会输出
  `zhihu:id:<whoami.id>`；operator 必须向 `PATCH /accounts/<account_id>` 提交
  `{"external_account_id":"zhihu:id:<whoami.id>"}`，服务将其写入 Account.profile，命令本身不会写数据库。Agent exact 计划会公开并绑定
  这个稳定身份，写入前再次执行 `whoami` 比对；缺失或不一致时在 article 子进程启动前失败。
  若 matching marker/URL 已出现但进程随后非零，任务保持 unknown/non-retryable，同时把候选
  post ID/URL 留给人工 reconciliation，不把它升级为 SUCCESS。
  `POST /accounts/{id}/login` 在 CLI 为首选时只验证该 HOME 的既有登录态，不会显示二维码，
  也不会自动改走 Playwright 登录。
- **控制面回执**：worker 在进入外部写之前为 job 持久化 operation ID；CLI 在返回前、worker 在
  数据库 finalize 前都会把 post identity 与脱敏状态原子写入私有 sidecar。正常落库后删除；若
  进程崩溃或 finalize 失败，stale reconciliation 会恢复 ID/URL 并保持禁止重发。正文、stdout、
  cookie、OAuth token 和任意未列入白名单的 raw 字段不会进入 sidecar。
- **YouTube**：每个账号的 `client_secrets.json`/`request.token` 放在权限
  `0700/0600` 的隔离目录；正文元数据写入临时 `0600` JSON，不进 argv。token 内容不进 argv，
  但 `request.token` 的文件路径会作为 `-cache=...` 参数传给子进程。合法回执中的 video ID 与
  请求 privacy 一致时确认成功，即使进程随后非零或超时也不重传；privacy 不一致时记录
  `effect_applied` 并转人工对账；无回执且进程已启动时进入 unknown。该路径当前只用于 legacy
  canary；由于缺少可审计的只读 channel identity，Agent exact renderer 已暂停。
- **GitHub Pages**：dry-run 明确 `effect_applied=false`；图片只能来自
  `GITHUB_PAGES_ASSET_ROOT`，拒绝隐藏文件、符号链接、越界和伪造扩展名，并受单张/总大小
  上限约束。live 的 clean/write/build/commit/push 全程持有跨进程仓库锁，commit 路径集合
  必须与本任务完全相等。push 后的 remote SHA 只是 source branch receipt，不是 Pages 部署
  完成或公网 URL 可访问的证据。commit 产生前的 build/add/commit 失败只会在 HEAD、index 和
  本轮文件指纹仍可证明安全时精确回滚本轮路径；发现用户并发改动就停止自动恢复。

指标回采绑定实际完成 fallback 链的 `publisher_kind`。只有同 kind 且显式声明支持 metrics 的
adapter 才能采集；CLI 没有真实 collector 时返回 `skipped`，不能用全 0 污染热度或账号健康。

这些 CLI 的独立 HOME、OAuth/token 和 cookie/profile 文件不在数据库 Fernet 加密边界内；
必须依靠 `0700/0600` 权限、专用主机账号和备份策略保护。
