# 平台能力矩阵

这是对外能力状态的唯一信源。代码里存在 Publisher 类或 mock 测试，不代表当前平台 UI 上能稳定真发。

## 成熟度定义

| 等级 | 入选条件 |
|---|---|
| **Stable** | 有可重复的真实登录、发布和结果校验；锁定上游版本；最近 30 天有 canary；失败语义与运维文档齐全 |
| **Beta** | 有脱敏、可复核的真实发布 evidence card 和自动化契约测试，但验证已超过 30 天或指标/运维仍不完整 |
| **Experimental** | 适配器或外部工具集成已存在，有 mock/dry-run 或历史描述，但没有满足 Beta 条件的真平台 evidence card |
| **Stub** | 部分实现、占位 selector 或首次真发待校准；不应用于真实运营 |

**当前 Stable 平台：无。**
**当前 Beta 平台：无。** 仓库里的历史真发描述缺少可独立复核的 evidence card，补齐前统一按
Experimental 对外呈现。

## 发布与指标

| 平台 | 成熟度 | 实现 | 已有自动化证据 | 指标回流 | Last evidence note |
|---|---|---|---|---|---|
| 小红书 | **Experimental** | SAU + XhsSkills；Camoufox 可选 | Registry/fallback/unknown 契约；CLI exit 0 或浏览器 success toast 无严格公开 note ID/URL 均不算 SUCCESS；历史真发描述无完整 evidence card | 未实现平台级采集 | 2026-05-17（历史描述；本次未复测） |
| 头条号 | **Experimental** | 自建 Playwright publisher | 全 mock 发布/selector/结果解析契约；历史真发描述待补证 | 已实现代码路径，仅有 mock 契约证据 | 2026-05-17（历史描述；本次未复测） |
| 知乎 | **Experimental** | `pyzhihu-cli==0.2.4` gated canary + Playwright fallback | 固定 commit 源码审计；fake CLI/结果不确定/fallback/超时契约；历史真发描述待补证 | 已实现代码路径，仅有 mock 契约证据 | CLI：2026-08-10 E2 源码审计；真发未复测 |
| 抖音 | **Experimental** | social-auto-upload wrapper | wrapper/unknown/mock 契约 | 未实现；SAU 无 post id/url 时不确认成功 | — |
| B 站 | **Experimental** | social-auto-upload wrapper | wrapper/unknown/mock 契约 | 未实现 | — |
| 快手 | **Experimental** | social-auto-upload wrapper | wrapper/unknown/mock 契约 | 未实现 | — |
| 视频号 | **Experimental** | social-auto-upload HTTP wrapper | wrapper/mock 契约；HTTP 能力受上游约束 | 未实现 | — |
| TikTok | **Stub** | 当前无注册 Publisher；官方 Content Posting API 是后续候选 | 无发布契约 | 未实现 | — |
| YouTube | **Experimental** | `youtubeuploader v1.25.5` gated canary；无 SAU fallback | 固定 tag 源码审计；fake CLI/OAuth 隔离/receipt/partial/unknown 契约 | 未实现 | CLI：2026-08-10 E2 源码审计；真发未测 |
| GitHub Pages / 静态站点 | **Experimental** | Hexo + 固定 `pnpm|npx`/git argv；尚未集成 `gh` | dry-run 无副作用；受控图片解码/大小；仓库锁；source-branch SHA/read-after-push/unknown 契约，不含 Pages 部署/live URL 验证 | 未实现 | 2026-08-10 离线契约；未连真实 remote/Pages |
| 微信公众号 | **Stub** | persistent-context draft-only publisher；不执行群发 | mock 草稿/unknown 契约；selector 与后台回执未跑专用账号 canary | 未实现 | — |
| 百家号 | **Stub** | default-off Playwright publisher（`BAIJIAHAO_PUBLISHER_ENABLED`） | mock 契约；selector 待首次真发校准 | 代码路径已有，未真平台验证 | — |
| 搜狐号 | **Stub** | default-off Playwright publisher（`SOHUHAO_PUBLISHER_ENABLED`） | mock 契约；selector 待首次真发校准 | 代码路径已有，未真平台验证 | — |

`Last evidence note` 只记录本仓库能找到的最后一次历史声明日期。它不是当前成功承诺，也不代表
该声明已满足下方 evidence card 规则。本次开源收口没有使用真账号复测。

知乎 CLI 当前默认关闭。审计对象是第三方
[`BAIGUANGMEI/zhihu-cli@8e32b99`](https://github.com/BAIGUANGMEI/zhihu-cli/commit/8e32b99e1883eaa0842653993618937a262817b6)，
不是知乎官方工具。0.2.4 写命令没有 JSON/内容文件/idempotency contract；只有返回码为 0、成功
marker 与纯数字文章 URL 三者一致时才确认成功。写子进程启动后的未确认结果会停止 fallback 和
自动重试，要求人工先核验平台。完成专用账号 canary 前不得升级 Beta 或默认开启。

YouTube CLI 当前也默认关闭。审计对象是
[`porjo/youtubeuploader@v1.25.5`](https://github.com/porjo/youtubeuploader/releases/tag/v1.25.5)。
只有 `-metaJSONout` 中的合法 video ID 才能确认已创建；ID 与请求 privacy 一致时，即使进程
非零/超时也确认该副作用并保留 reconciliation 标记。有 ID 但 privacy 不匹配时保留 post identity、
返回失败并转人工对账；无 ID 而子进程已启动时禁止 fallback/自动重传。上游没有 auth-only 命令，
首次 OAuth 需由人在可信终端预置，可能与第一次 private canary 上传绑定。
专用频道 private canary 前不得升级 Beta 或默认开启。

worker 的三类停止条件是成功、已发生副作用、结果未知；只有明确无副作用的确定失败才会 fallback。
迁移后的 CLI/Git 路径以及直接浏览器 Publisher 已在 mock 契约中建模最终写入边界：点击后异常、
无 ID 或 teardown 失败不会触发盲目重试。但 mock 不能证明 selector 与平台服务端行为仍然有效，
所以浏览器路径继续保持 Experimental/Stub，直到有真实 canary evidence card。

## 证据升级规则

把平台从 Experimental/Stub 升级到 Beta，PR 必须附上：

1. 上游工具仓库与精确 commit/tag。
2. OS、浏览器/引擎版本、账号类型和内容类型。
3. 从登录到平台端可见结果的可复现步骤，以及失败路径。
4. 平台 post id/url 或等价服务端证据。不得提交 cookie、token、真实账号信息或截图中的个人数据。
5. 更新本矩阵的 `Last evidence note`、指标能力和已知限制。

Stable 还需要连续 canary、可观测的成功率、幂等/重复发布防护和已发布版本的运维承诺。
