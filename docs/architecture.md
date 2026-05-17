# 架构设计

## 五端闭环水线（端到端视图）

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       ai-ops-auto · 五端闭环发布水线                        │
└─────────────────────────────────────────────────────────────────────────────┘

     ① 内容池端              ② 中控生成端           ③ 数据中枢端
   ┌──────────────┐       ┌──────────────────┐    ┌──────────────────┐
   │ Topic 主题库  │──────▶│ ContentGenerator │───▶│ Article 状态机   │
   │  · 关键词    │       │  · LLM Driver    │    │ draft→ready→     │
   │  · 人设画像  │       │  · 多平台调性    │    │ scheduled→...    │
   │  · 热度分    │◀──────│ VideoEngine(MPT) │───▶│ Asset 物料库     │
   │              │ 反馈   │  · 主题→视频    │    │  · image/video   │
   └──────────────┘       └──────────────────┘    └──────────────────┘
                                                          │
                                                          ▼
     ⑤ 运行管理端                                  ④ 触发引擎端
   ┌──────────────────┐                          ┌────────────────────┐
   │ PublishJob 队列  │◀─────── Scheduler ──────▶│ PublisherRegistry  │
   │  · 调度/重试    │           (APScheduler)   │  按优先级 fallback  │
   │  · attempts<=N  │                          │ ┌───────────────┐  │
   │ ┌────────────┐  │                          │ │ SAU (主力)    │  │
   │ │ Metrics 采集│ │ ◀──── 数据回流 ───────────│ │ XHS Skills    │  │
   │ │  ·赞 评 转 │  │                          │ │ Zhihu(自建)   │  │
   │ │  ·健康检查 │  │                          │ │ Toutiao(自建) │  │
   │ └────────────┘  │                          │ └───────────────┘  │
   │ Account 多账号  │ ◀─── 风控降权 ────────────│   Fernet 凭证      │
   └──────────────────┘                          └────────────────────┘
                                                          │
                                                          ▼
                                            ┌──────────────────────────┐
                                            │   外部工具引擎层（不写） │
                                            │  · social-auto-upload    │
                                            │  · MoneyPrinterTurbo     │
                                            │  · XiaohongshuSkills     │
                                            └──────────────────────────┘
                                                          │
                                                          ▼
                                            ┌──────────────────────────┐
                                            │  小红书·抖音·快手·B站   │
                                            │  视频号·头条·知乎·...    │
                                            └──────────────────────────┘
```

**闭环点**：⑤运行管理端的 Metrics 采集结果回流到 ①内容池端 的 `heat_score`，
驱动 ②中控生成端 下一轮选题——这是真正的"运营自动化飞轮"，不只是单向分发。

## 顶层逻辑

本项目定位为 **编排层（Orchestration Layer）**，不重复造发布器和视频剪辑的轮子。

三层分工：

| 层级 | 角色 | 我们的关系 |
|------|------|-----------|
| **编排层** | 内容/账号/调度/审核/数据回流 | ✅ 我们写 |
| **引擎层** | 各平台发布、视频剪辑、AI 生成 | 🔌 集成外部开源 |
| **基础层** | DB、队列、文件系统、浏览器 | ⬜ 标准基础设施 |

## 核心抽象

### 1. PublisherBase（发布器抽象）

所有平台发布通过统一接口，底层可换工具：

```python
class PublisherBase(ABC):
    platform: Platform

    @abstractmethod
    async def login(self, account: Account) -> bool: ...

    @abstractmethod
    async def publish(self, account: Account, content: Content) -> PublishResult: ...

    @abstractmethod
    async def health_check(self, account: Account) -> AccountHealth: ...
```

实现策略：
- `SocialAutoUploadPublisher`：调 social-auto-upload 的 CLI，覆盖 7 个平台
- `XhsSpecializedPublisher`：小红书加固版，调 xhs-toolkit
- `ShortVideoAutoPublisher`：覆盖头条/百家号

通过 `PublisherRegistry` 按 `Platform → Publisher` 路由，支持优先级 + fallback。

### 2. VideoEngineBase（视频引擎抽象）

```python
class VideoEngineBase(ABC):
    @abstractmethod
    async def render(self, brief: VideoBrief) -> VideoArtifact: ...
```

实现：
- `MoneyPrinterEngine`：主题→自动出视频（口播/混剪）
- `NarratoEngine`：解说类（电影解说、纪录片）
- `FFmpegRawEngine`：手动剪辑兜底（用户已有素材时）

### 3. ContentGeneratorBase（内容生成抽象）

```python
class ContentGeneratorBase(ABC):
    @abstractmethod
    async def generate(self, topic: Topic, profile: AccountProfile) -> Article: ...
```

LLM 解耦：OpenAI / Anthropic / DeepSeek / 通义 都是 driver，配置切换。

## 状态机

```
   [draft] ──写完──→ [ready] ──排程──→ [scheduled]
                                            │
                                            ↓
                                     [publishing]
                                       │     │
                            success ←──┘     └──→ [failed] ──重试──┐
                              │                                     │
                              ↓                                     │
                       [published] ←───────── 重试上限 ─── [dead]   │
                              │                                     │
                              ↓                                     │
                       [metrics_collecting] ──回流──→ [closed]      │
                                                                    │
                                              ←───────────────────┘
```

关键约束：
- 状态变更必须落库 + 写审计日志
- 失败可重试 N 次，超限进死信
- 数据采集是独立异步阶段，不阻塞发布

## 数据模型

| 实体 | 字段要点 |
|------|---------|
| `Topic` | 主题名、关键词、人设画像、目标平台、热度分 |
| `Article` | 关联 topic，标题/正文/物料、状态、目标平台、目标账号、计划发布时间 |
| `Asset` | 类型（image/video/audio）、本地路径、来源（用户/AI生成）、metadata |
| `Account` | 平台、昵称、cookie 加密blob、状态、风控等级 |
| `PublishJob` | 关联 article + account，状态、重试次数、平台返回 id、发布 URL |
| `Metrics` | 关联 PublishJob，点赞/评论/转发/曝光，时序快照 |

## 多账号策略

- **隔离**：cookie 单独加密存储（Fernet），按 account_id 取
- **限流**：每账号每平台独立限流计数（每日/每小时）
- **风控感知**：连续失败自动降权，触发健康检查
- **轮换**：同平台多账号场景下，按权重轮询 + 内容查重

## 调度

第一版：**APScheduler**（in-process，零外部依赖）。
- 文章排程：定时触发 publishing
- 健康检查：每日扫账号
- 数据采集：发布后 1h/24h/7d 三次采集

切 Celery 触发条件：
- 任务量 > 1k/day
- 需要跨机器分布式
- 视频剪辑要 GPU 节点

## 集成外部工具的方式

详见 [external-tools.md](external-tools.md)。三种方式：

1. **subprocess CLI**：调用外部工具的命令行（social-auto-upload）
2. **HTTP API**：外部工具作为独立服务（MoneyPrinterTurbo 的 API 模式）
3. **Python import**：少数工具打了 pip 包

优先级：HTTP > subprocess > import。隔离性越好越优先。

## 风控与合规

- 内容查重：同账号 7 天内 simhash 相似度阈值
- 频率控制：单账号单平台每日上限可配
- 审核钩子：可插入 LLM 审核 / 人工 review 队列
- 敏感词：可配置词库 + 命中阻断

## 数据回流闭环

```
发布完成 → 采集互动 → 主题热度更新 → 生成器策略调整 → 下一轮选题
```

这是真正的"运营自动化"——不只是分发，是有反馈的飞轮。

## 扩展性

新加平台只需：
1. 在 `Platform` 枚举加值
2. 实现一个 `PublisherBase` 子类（或扩展 `SocialAutoUploadPublisher` 的平台映射）
3. 在 `PublisherRegistry` 注册

新加视频引擎同理。
