# 外部工具集成清单

本项目所有发布与视频生成能力均依赖以下开源工具。不重复造轮子是底层逻辑。

## 发布类

### 1. social-auto-upload（多平台主力）

- **仓库**：https://github.com/dreammis/social-auto-upload
- **覆盖**：抖音、快手、小红书、视频号、B站、TikTok、YouTube
- **技术栈**：Python + Playwright + SQLite
- **集成方式**：git submodule + subprocess CLI 或 HTTP（自带 sau_frontend）
- **配置点**：
  - cookie 存储路径（我们镜像到 `data/cookies/<platform>/<account>.json`）
  - 平台开关 / 限速

### 2. xhs-toolkit（小红书加固 fallback）

- **仓库**：https://github.com/aki66938/xhs-toolkit
- **覆盖**：小红书图文/视频笔记，支持定时、AI 生成、cron
- **形态**：MCP server
- **何时启用**：social-auto-upload 在小红书风控失败时自动 fallback

### 3. xhs_ai_publisher（小红书完整版备选）

- **仓库**：https://github.com/BetaStreetOmnis/xhs_ai_publisher
- **特点**：PyQt 桌面 UI + FastAPI 服务 + 登录态复用
- **何时启用**：需要桌面端可视化运营的场景

### 4. ShortVideo.AutoPublisher（头条/百家号补位）

- **仓库**：https://github.com/dorisoy/ShortVideo.AutoPublisher
- **覆盖**：抖音、百家号、小红书、视频号、头条
- **注意**：C# 项目，集成成本较高，仅当 social-auto-upload 不覆盖时启用

### 5. Douyin-Tiktok-Uploader（抖音兜底）

- **仓库**：https://github.com/hyqshr/Douyin-Tiktok-Uploader
- **技术栈**：PyAutoGUI（桌面自动化，需 GUI 环境）
- **何时启用**：浏览器自动化失效时的最后兜底

## 视频生成类

### 1. MoneyPrinterTurbo（主力，13K⭐）

- **仓库**：https://github.com/harry0703/MoneyPrinterTurbo
- **能力**：主题/关键词 → 文案 + 素材匹配 + 字幕 + BGM + 合成
- **技术栈**：ImageMagick + MoviePy + FFmpeg
- **接入**：MVC API + Web UI 都可用，我们用 API 模式
- **配置点**：LLM key、素材来源（pexels/pixabay）、配音引擎

### 2. NarratoAI（解说类，8K⭐）

- **仓库**：https://github.com/linyqh/NarratoAI
- **能力**：电影/纪录片解说自动剪辑，逐帧分析（v0.7.8 起）
- **技术栈**：Streamlit + LLM
- **何时启用**：需要做"解说类"内容时

## 安装与运维

### git submodule 方式（推荐）

```bash
# 在项目根目录
git submodule add https://github.com/dreammis/social-auto-upload external/social-auto-upload
git submodule add https://github.com/harry0703/MoneyPrinterTurbo external/MoneyPrinterTurbo
git submodule update --init --recursive
```

### 独立部署方式（生产推荐）

每个外部工具单独跑容器/进程，通过 HTTP 或 CLI 调用：

```
docker-compose.external.yml:
  - social-auto-upload      :8001
  - money-printer-turbo     :8080
  - xhs-toolkit (MCP)       :8765
```

我们的 `config.py` 用 `EXTERNAL_*_URL` / `EXTERNAL_*_PATH` 切换。

## 版本锁定与升级策略

- submodule 锁定 commit SHA，升级走 PR
- 外部工具 API 变更视为破坏性变更，需更新 wrapper + 加回归测试
- 建议每月跟一次上游，看 release notes 评估升级 ROI

## 不集成的工具（决策记录）

| 工具 | 不集成原因 |
|------|-----------|
| cv-cat/Spider_XHS | 采集向，本项目重发布；后续做数据回流时再评估 |
| XHS-Downloader 类 | 用途不匹配 |
| 单平台爬虫类 | 与发布主线无关 |
