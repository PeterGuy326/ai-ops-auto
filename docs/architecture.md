# 架构设计

## 定位

`ai-ops-auto` 是 Agent-native Creator Ops **控制面**，不是内容大模型、通用 Agent 或平台上传引擎。

```text
Agent / operator
      |
      | CLI / HTTP API / future MCP
      v
+---------------------- control plane ----------------------+
| content state | approval | accounts | policy | job ledger |
+---------------------------+--------------------------------+
                            |
                            v
                    single worker process
              scheduler | claim | retry | metrics
                            |
                            v
                    Publisher Registry
                            |
              +-------------+--------------+
              |                            |
      built-in browser adapters     external tools/APIs
              |                            |
              +-------------+--------------+
                            v
                     content platforms
```

## 角色分工

| 层 | 拥有的决策/状态 | 不拥有 |
|---|---|---|
| Agent（Codex / Claude / custom） | 选题、内容生成、计划、异常诊断、绩效复盘 | 长期任务真相、凭证存储；目标设计中也不拥有审批绕过权 |
| API/UI | 控制面读写、审批和显式管理操作 | 调度器所有权 |
| Worker | 调度 owner、到期任务扫描、claim、执行、重试与已实现的指标回采 | 内容审批决策 |
| Database | Topic/Article/Asset/Account/PublishJob/MetricsCollectionTask/Metrics 的持久化真相 | 平台页面的实时真相 |
| Publisher | 单平台登录、发布、结果校验和可选 metrics 翻译 | 跨账号策略和任务排程 |
| 人 | 不可逆发布的授权、登录与风控处置 | 每次重新编排全部技术细节 |

当前有两套明确分离的身份面：legacy 审批、分发和显式执行端点仍使用全权限管理 `API_KEY`；
Agent contract v1 使用带最小 scope 的独立 Bearer principal，只有 `type=human` 能读取/决定审批，
且请求者与计划创建者不能自签。后者能证明不同凭证身份参与，不能单靠软件证明凭证背后一定是真人；
部署时仍须用 SSO、审批系统、硬件密钥或等价托管边界隔离 human token。

## 核心数据

| 实体 | 用途 |
|---|---|
| `Topic` | 选题、关键词、人设、目标平台和热度 |
| `Article` | 平台无关的内容与审核/发布状态 |
| `Asset` | 图片、视频、音频、字幕与文档资产 |
| `Account` | 平台账号、数据库内加密的 credential blob、健康和配额；外部 profile 文件不在该加密边界内 |
| `PublishJob` | 一个 Article 向一个 Account 发布的持久化执行记录 |
| `MetricsCollectionTask` | 一个成功 job 的固定 1h/24h/7d 回采意图、lease、重试和截止时间 |
| `Metrics` | 关联 PublishJob 的带来源时序快照 |

v1 还持久化 `PublicationPlan`、`ApprovalRequest` 与 `AgentOperation`，分别保存不可变计划/摘要、
独立审批证据和幂等请求账本；legacy Article 状态流继续兼容已有运营界面。

Article 与 Job 是两层状态：一篇 Article 可以扇出多个 PublishJob，不应用某一个平台的结果
覆盖其他账号的真相。

```text
Article: DRAFT -> READY -> SCHEDULED -> PUBLISHING -> PUBLISHED
                                             \----> FAILED / DEAD

Job:     PENDING -> RUNNING -> SUCCESS
                    |   \----> RETRYING -> RUNNING
                    \--------> FAILED / DEAD

Metrics task: QUEUED -> CLAIMED -> SUCCEEDED
                    |      \----> QUEUED
                    \-----------> FAILED
```

具体迁移以模型和 worker 代码为准；文档不应虚构代码里不存在的 `metrics_collecting`
或 `closed` 状态。

## API 与 worker 分进程

API lifespan **不启动 APScheduler**。部署单元包含：

```bash
ai-ops serve   # HTTP API / UI
ai-ops worker  # 唯一调度 owner
```

分离原因：

- API worker 数量不应决定调度器数量。
- Web 进程重启不应丢失持久化任务。
- 唯一 worker owner 让 cron、扫描和日志语义更清晰；任务级原子 claim 仍用来防止重入。

这里的“唯一”是当前部署约束，不是分布式选主保证。代码尚未实现数据库 leader lease；如果误启
多个 worker，PublishJob 的原子 claim 可以保护同一轮发布入口，但 cron 仍可能重复注册。

`AUTO_PUBLISH_ENABLED=false` 时 worker 保持运行，但不执行自动发布扫描。已授权且已成功发布内容的
到期指标任务、账号健康和报表 cron 仍会运行。该开关不应被解释为“所有管理端点或外部读取都无副作用”。

## 调度与执行不变量

- 数据库中的 `PublishJob` 与 `MetricsCollectionTask` 是任务真相；APScheduler 承载 cron 和发布
  唤醒优化，发布后指标不再注册一次性 date callback。
- worker 启动时会重新发现已到期的持久化任务，不依赖上个进程的内存 job。
- 任务在执行平台副作用前必须原子 claim；不符合可执行状态时立即返回。
- 重试是持久化时间，基础退避由 `JOB_RETRY_BASE_SECONDS` 控制。
- 指标任务使用独立的 collection timeout、lease、重试和并发配置；任务的原始 `due_at` 不随重试
  改动，超出固定窗口截止时间后明确失败，不用迟到的当前快照冒充历史窗口数据。
- 扫描节奏由 `SCHEDULER_POLL_SECONDS` 控制；它不是精确到秒的执行 SLA。
- 同时执行数由 `SCHEDULER_MAX_CONCURRENCY` 限制，单次外部调用由
  `JOB_EXECUTION_TIMEOUT_SECONDS` fail-closed。
- 一次性任务按 UTC 解释；健康检查/报表 cron 使用显式 `SCHEDULER_TIMEZONE`。
- 当前只实现 `apscheduler`；`celery` 不是可用后端。

当前恢复边界必须明确：scanner 自动执行到期的 `PENDING`/`RETRYING`。进程若在平台调用期间
硬退出，超过 `JOB_RUNNING_TIMEOUT_SECONDS` 的 `RUNNING` 会 fail-closed 到需要人工处置的失败
状态，不会直接盲重试。平台端仍可能已经成功；在实现 claim lease、平台幂等键和自动
reconciliation 前，运营者必须先核对平台真实状态。

## Publisher 契约

`PublisherBase` 把平台差异收口为：

```python
async def login(account_id, credential) -> bool: ...
async def publish(account_id, credential, content) -> PublishResult: ...
async def health_check(account_id, credential) -> AccountHealth: ...
async def collect_metrics(post_id, post_url, credential) -> dict: ...
```

`PublisherRegistry` 可以为一个 Platform 注册多个实现并按优先级 fallback。worker 遇到以下任一
结果都会停止尝试下一实现：`success=True`、`effect_applied=True` 或
`outcome_uncertain=True`。只有适配器明确返回“失败且无副作用、结果也确定”的预检失败，才允许
fallback。它只能提高技术容错，不能代替幂等性、服务端成功验证或平台能力证据。

这套边界取决于 Adapter 如实报告结果。目前迁移过的 CLI/Git 路径已使用上述字段；旧浏览器
Adapter 仍是 Experimental，异常后的副作用判定并不都具备同等证据，不能把该保证外推到所有平台。

第三方 Publisher 使用 `ai_ops.publishers.v1` entry point。安装包只有同时命中精确的
`distribution:entry-point` allowlist，才会被加载；manifest、factory 和每次构造出的实例都会核验
API version、platform、namespaced kind、metrics 与 exact renderer 自描述。同平台不同 kind
可以共存；重复 `(platform, kind)` 会拒绝全部冲突插件。已启用插件校验失败时 registry 保持可导入，
供 API/doctor 暴露诊断，但所有 Publisher routing fail closed，不会静默换实现。

这仍是同进程信任边界：插件能访问进程权限，并会在 Publisher 方法调用时接触账号数据。allowlist
只是代码执行授权，不提供沙箱。不可信社区适配器未来必须进入独立 subprocess/RPC host 和按能力
发放凭证的 broker。完整契约见 [Publisher Plugin SDK v1](publisher-plugins.md)。

对外支持等级只看 [平台能力矩阵](platform-capabilities.md)。

## 外部工具边界

支持三种 Adapter 形式：

1. subprocess CLI，例如版本门禁的知乎 canary、social-auto-upload/FunClip。
2. HTTP API，例如外置视频生成服务。
3. 少量受信任 Python 调用，包括显式 allowlist 的 Publisher 插件。

外部工具不是本仓库的依赖锁定一部分；真实发布证据必须同时记录它们的精确版本。
数据库中的 credential blob 由 Fernet 保护，但知乎独立 HOME、YouTube OAuth 文件、SAU cookie
镜像和浏览器 persistent profile 是部署机上的独立敏感状态，只受目录/文件权限与主机隔离保护，
不会被 `FERNET_KEY` 自动加密。

## 指标闭环的现实边界

理想闭环是：

```text
publish -> verify post identity -> collect normalized metrics -> update topic signal -> agent review
```

但只有 Publisher 返回可校验的 post id/url，并且实现真实 `collect_metrics`，这条链路才成立。
大部分平台目前没有这一证据，因此不应对外声称已完成跨平台数据飞轮。

成功发布会以 `finished_at` 为锚点，在数据库中固定建立 1h/24h/7d 三个任务。1h、24h、7d
分别允许最多额外 1h、6h、24h 的采集宽限；到期 worker 通过条件 claim 和 expiring lease 执行，
重启后由扫描器恢复。任务快照用唯一外键绑定，旧 lease owner 不能补写或重复落库。

新的 24h task 用绑定 `window_seconds=86400` 的快照做当次健康判断，手动采集和更晚的 7d 数据
不会替代本次窗口证据；没有 task-bound 历史快照的 legacy job 仍保留旧的 latest-metric fallback。
账本只保证控制面的排程、恢复和数据库快照去重，不保证外部读取 exactly once：collector 返回后、
事务提交前的崩溃可能使恢复任务再次读取平台数据。Publisher 仍必须提供可验证 post identity 和真实
`collect_metrics`，因此这仍不是跨平台数据飞轮完成声明。

## 部署拓扑

当前推荐最小拓扑：

```text
reverse proxy/TLS
      |
      v
 one API process ---- PostgreSQL/SQLite
                           ^
                           |
                    one worker process
                           |
                  browsers/external tools
```

在完成多实例数据库和调度验证前，不要把 API/worker 扩成多副本并宣称已支持分布式运行。
操作步骤见 [部署指南](deployment.md)。
