# ai-ops-auto

AI 运营自动化中台。一句话：**内容生产 → 视频剪辑 → 多平台多账号分发** 全链路自动化。

> 本项目是 **编排层**，发布和剪辑都集成成熟开源工具，不重复造轮子。

## 能做什么

- 📚 **多主题文章库**：按主题归档，状态机驱动（草稿 / 待发 / 已排程 / 已发布 / 失败）
- 🎬 **视频自动生成**：调用 [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) / [NarratoAI](https://github.com/linyqh/NarratoAI)，主题 → 文案 → 素材 → 字幕 → 配音 → 合成
- 🚀 **多平台分发**：通过 [social-auto-upload](https://github.com/dreammis/social-auto-upload) 覆盖抖音、小红书、视频号、快手、B站、TikTok、YouTube
- 🌶️ **小红书专项加固**：fallback 到 [xhs-toolkit](https://github.com/aki66938/xhs-toolkit) / [xhs_ai_publisher](https://github.com/BetaStreetOmnis/xhs_ai_publisher)
- 👥 **多账号管理**：账号池 + cookie 加密存储 + 健康检查
- ⏰ **调度系统**：APScheduler 起步，可平滑切 Celery+Redis
- 📊 **数据回流**：发布后采集互动数据，主题热度反馈给生成器

## 不做什么（重要）

- ❌ 不自己写小红书 / 抖音的反爬 / 签名 / 上传逻辑（集成现有开源工具）
- ❌ 不自己实现视频剪辑算法（FFmpeg + MoneyPrinterTurbo 是足够好的轮子）
- ❌ 不绑定单一 LLM（OpenAI / Anthropic / DeepSeek / 通义 都是配置项）

## 架构总览

```
┌─────────────── 我们写的（编排层）──────────────────┐
│  API (FastAPI) │ Scheduler │ Content │ Accounts    │
│      ↓             ↓           ↓          ↓         │
│           Adapter / Wrapper / Registry              │
└────────────────────────┬───────────────────────────┘
                         │ subprocess / HTTP
        ┌────────────────┴─────────────────┐
        ↓                                  ↓
┌─── 外部工具（引擎层）────┐   ┌─── 基础设施 ────┐
│ social-auto-upload       │   │  SQLite/PG      │
│ MoneyPrinterTurbo        │   │  Redis (opt)    │
│ xhs-toolkit              │   │  FFmpeg         │
│ NarratoAI                │   │  Browser (Edge) │
└──────────────────────────┘   └─────────────────┘
```

详见 [docs/architecture.md](docs/architecture.md)。

## 快速开始

```bash
# 1. 安装本项目
pip install -e .

# 2. 安装外部工具（按需）
bash scripts/install_external.sh   # 拉取 social-auto-upload / MoneyPrinterTurbo 等

# 3. 配置环境
cp .env.example .env
# 编辑 .env 填入 LLM key、外部工具路径、加密 key

# 4. 初始化数据库
python scripts/init_db.py

# 5. 启动 API
uvicorn ai_ops.api.main:app --reload
```

## 目录结构

```
ai-ops-auto/
├── docs/                       # 架构、外部工具、路线图
├── src/ai_ops/
│   ├── core/                   # 枚举 / ORM / Schema / DB
│   ├── content/                # 主题、文章、AI 生成
│   ├── accounts/               # 多账号管理 + 加密
│   ├── video/                  # 视频引擎适配器
│   ├── publishers/             # 平台发布适配器
│   ├── scheduler/              # 调度 + 任务执行
│   └── api/                    # FastAPI
├── tests/
├── scripts/                    # 初始化、安装外部工具
└── data/                       # 素材、产物、本地 DB
```

## 集成的外部工具清单

详见 [docs/external-tools.md](docs/external-tools.md)。

## 路线图

- [x] 顶层架构 + 编排层骨架
- [ ] social-auto-upload 集成 + e2e 跑通一个平台
- [ ] MoneyPrinterTurbo 集成 + 一键出视频
- [ ] 多账号 cookie 池 + 健康检查
- [ ] 数据回流 + 主题热度反馈
- [ ] Web 管理 UI
