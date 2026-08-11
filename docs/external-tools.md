# 外部工具边界

`ai-ops-auto` 保存内容、账号策略和任务状态，但不自己实现所有平台上传、浏览器兼容或视频处理。
外部工具是可选 Adapter，不会因为本仓库能调用它们就自动变成 Stable 平台能力。

平台支持现状见 [平台能力矩阵](platform-capabilities.md)。

## 当前代码中的集成

| 工具/服务 | 上游 | 本项目中的边界 | 默认是否就绪 |
|---|---|---|---|
| social-auto-upload（SAU） | [dreammis/social-auto-upload@008e4ff](https://github.com/dreammis/social-auto-upload/commit/008e4ff66abdf48eb1f4b999272ef979711af436) | `SocialAutoUploadPublisher` 通过 subprocess CLI 或 HTTP 翻译发布请求；无 post identity 时只记 unknown | 否 |
| pyzhihu-cli 0.2.4 | [BAIGUANGMEI/zhihu-cli](https://github.com/BAIGUANGMEI/zhihu-cli/tree/8e32b99e1883eaa0842653993618937a262817b6) | `ZhihuCliPublisher` 的版本门禁 canary；CLI 预检失败才回退 Playwright | 否（feature flag 默认关闭） |
| youtubeuploader v1.25.5 | [porjo/youtubeuploader](https://github.com/porjo/youtubeuploader/releases/tag/v1.25.5) | `YoutubeUploaderPublisher` 通过官方 Data API 上传，以 `-metaJSONout` video ID 作回执 | 否（feature flag 默认关闭） |
| XiaohongshuSkills | [white0dew/XiaohongshuSkills](https://github.com/white0dew/XiaohongshuSkills) | `XhsSkillsPublisher` 的小红书可选 fallback | 否 |
| MoneyPrinterTurbo | [harry0703/MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) | `MoneyPrinterEngine` 的 HTTP/外置引擎；CLI 仅使用仓库内显式配置的独立 venv | 否 |
| FunClip | [modelscope/FunClip](https://github.com/modelscope/FunClip) | ASR + 文字稿视频切片；仅使用仓库内显式配置且可验证的独立 venv | 否 |
| Kling API | 可灵开放 API | `KlingEngine`；需用户自己的 AK/SK 和服务配额 | 否 |
| ListenHub/Marswave API | ListenHub OpenAPI | `ListenHubProvider`；需用户自己的 API key 和服务配额 | 否 |
| HappyHorse 兼容端点 | 用户提供 | 异步视频 job 协议适配；开源默认没有 endpoint/key | 否 |

此外，仓库包含若干自建 Playwright Publisher（头条、知乎、公众号、百家号、搜狐号）。
它们不是上表外部工具，但同样受平台 UI 和浏览器版本变化影响。

第三方 Python Publisher 可通过 [Plugin SDK v1](publisher-plugins.md) 接入。它只适合已经完成
供应链审阅的受信任包；allowlist 不会隔离进程环境或账号数据。社区 CLI/MCP 若不满足同进程信任
要求，必须继续使用固定 argv 的 subprocess/localhost RPC 边界，不能为了“插件化”直接 import。

### 知乎 CLI 的隔离安装

```bash
uv tool install 'pyzhihu-cli==0.2.4'
zhihu --version
ai-ops zhihu-login <account_id>
```

只接受已审计的 0.2.4。它是 Apache-2.0 的第三方 Alpha 工具，不是知乎官方 CLI；不要把它
安装进控制面 venv，也不要执行 `login --cookie`。ai-ops 会为每个 `account_id` 派生独立 HOME，
只通过显式终端二维码登录。正文仍位于 argv、富文本会被上游额外 `<p>` 包装，因此当前只适合
短内容测试账号 canary；配置和验收见 [Publishing SOP](publishing-sop.md)。

### YouTube CLI 的隔离安装

只使用审计过的
[`v1.25.5` release](https://github.com/porjo/youtubeuploader/releases/tag/v1.25.5)，并先核对：

```bash
youtubeuploader -version
# Youtubeuploader version: 1.25.5
```

不要在 worker 中触发首次 OAuth。上游没有 auth-only 命令；运维人员须在可信终端人工完成授权，
并明确知晓授权过程可能与第一次 private canary 上传绑定。随后为每个 `account_id` 预置两个
`0600` 文件：

```text
data/cli_profiles/youtube/account_<id>/
├── client_secrets.json
└── request.token
```

开启 `YOUTUBE_UPLOADER_ENABLED=true` 前，先在专用频道做 private canary。适配器固定
`-oAuthPort=-1`，凭证不完整时失败关闭，不在后台开浏览器或监听 OAuth 端口。

## 安装模式

### MoneyPrinterTurbo CLI 隔离

HTTP 模式优先。必须使用本地 CLI 时，在 MPT 仓库内创建独立 venv，并显式配置：

```bash
python3.11 -m venv external/MoneyPrinterTurbo/.venv
external/MoneyPrinterTurbo/.venv/bin/pip install -r external/MoneyPrinterTurbo/requirements.txt
```

```dotenv
EXTERNAL_MPT_PATH=./external/MoneyPrinterTurbo
MPT_PYTHON=./external/MoneyPrinterTurbo/.venv/bin/python
```

`MPT_PYTHON` 为空、不可执行或越出 `EXTERNAL_MPT_PATH` 时，CLI 模式失败关闭；不会查找
`PATH` 中的 `python`。MPT 子进程不继承控制面的 API/LLM/Fernet 密钥，也不加载浏览器
`sitecustomize`。

### 本地试用

```bash
bash scripts/install_external.sh
```

该脚本是开发便利工具，会访问网络并拉取多个第三方仓库。运行前先审查脚本和上游许可证。

当前脚本使用 shallow clone/拉取上游最新代码，**只适合本地开发，不是可重复生产供应链**。
不要把一次本地成功解读为后续上游版本也保证兼容。可审计部署不得跟随 HEAD；本轮 SAU
源码审计锚点是 `008e4ff66abdf48eb1f4b999272ef979711af436`，更新前必须重新检查许可证、命令契约和回执。

固定安装示例：

```bash
git init external/social-auto-upload
git -C external/social-auto-upload remote add origin https://github.com/dreammis/social-auto-upload.git
git -C external/social-auto-upload fetch --depth 1 origin 008e4ff66abdf48eb1f4b999272ef979711af436
git -C external/social-auto-upload checkout --detach FETCH_HEAD
```

### 可审计环境

1. 逐个审核上游许可证、维护状态和安全问题。
2. 把上游锁定到精确 commit/tag，记录与 Publisher 版本的对应关系。
3. 使用隔离 venv/容器，不要把体积大、版本冲突的视频工具混入控制面环境。
4. 对下载的模型和二进制校验来源/校验和，禁止在构建时注入真实凭证。
5. 在专用测试账号上跑真实 canary，更新能力矩阵的 `Last verified`。

## 运行方式

- **subprocess CLI**：适合 SAU/FunClip。控制面必须使用参数列表，不把未信任内容拼接成 shell。
- **HTTP API**：适合外置视频服务。需要超时、身份验证、网络出站控制与日志脱敏。当前 SAU
  HTTP 集成自身没有认证，只能绑定 loopback 或可信私网，不得直接暴露公网；该路径仅映射
  小红书、视频号、抖音和快手。
- **Python import**：只用于依赖可控、许可证清晰且显式 allowlist 的受信任轻量库；不可信工具
  等待未来独立 plugin host。

API 与外部工具之间不应传递本项目的管理 `API_KEY`。为每个外部服务使用单独、最小权限凭证。
现有 SAU/XhsSkills 子进程使用环境 allowlist，不继承 LLM key、`FERNET_KEY`
或管理 `API_KEY`；SAU cookie 镜像文件以 `0600` 原子替换。它们没有结构化 post ID，
因此子进程一旦启动，无论退出码都需先去平台对账，不会自动 fallback/重试。

Fernet 只覆盖数据库里的 credential blob。知乎独立 HOME、YouTube OAuth 文件、SAU cookie
镜像和浏览器 profile 是磁盘敏感状态，不会因配置了 `FERNET_KEY` 而自动加密；部署者需负责
目录权限、主机隔离、备份和销毁。

## Docker 边界

根目录 Dockerfile 不安装上述外部仓库或浏览器二进制。基础镜像能运行 API/worker 与离线控制面，
不能单独完成真平台发布。参见 [部署指南](deployment.md)。
