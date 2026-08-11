# 部署指南（Alpha）

`ai-ops-auto` 尚处于 Alpha。本文档给出当前有证据支持的自托管拓扑，不构成高可用或无人值守承诺。

## 支持的拓扑

API 和调度 worker 必须分进程：

```text
TLS reverse proxy
       |
       v
  one API process -------- database
                              ^
                              |
                       one worker process
                              |
                    browser/external tools
```

```bash
ai-ops serve
ai-ops worker
```

API lifespan 不启动 APScheduler。`ai-ops worker` 是唯一调度 owner，负责到期任务、健康检查和报表 cron。
`AUTO_PUBLISH_ENABLED=false` 时 worker 仍应运行：它不执行自动发布，但健康/报表任务仍可运行。

“唯一 worker”目前由部署者保证，项目尚无 leader lease。不要同时启动两个 worker；原子 job
claim 不能阻止健康检查、报表等 cron 被重复注册。

目前建议单 API + 单 worker。在完成多副本压力和故障恢复验证前，不要使用 Gunicorn 多 worker、
Kubernetes 多副本或把本项目描述为分布式系统。当前调度后端只有 APScheduler，不支持 Celery。

## 环境需求

- Python 3.11 或 3.12。
- Linux/macOS；建议先在目标 OS 上对适配器做 canary。
- SQLite 只用于单机低并发验证；长期自托管优先 PostgreSQL 14+。
- 只有真实浏览器发布时才需要 Chrome/Playwright/Camoufox 等平台工具。
- 视频产物可以很大；`data/` 需要持久化和容量监控。

## 关键配置

从 `.env.example` 开始，不要复用其他环境的 `.env`。

| 变量 | 要求 |
|---|---|
| `DATABASE_URL` | API 与 worker 必须指向同一数据库 |
| `FERNET_KEY` | 必须持久保存；只加密数据库 credential blob，丢失后无法解密；不覆盖磁盘 profile/OAuth/cookie |
| `API_KEY` | 非空强随机值；legacy 管理端点的全权限 key，不能提供给 Agent |
| `AGENT_PRINCIPALS` | `/v1` 独立 Bearer principal 的 JSON 数组；只保存 token SHA-256；Agent 与 human approver 必须分离 |
| `AGENT_ASSET_IMPORT_ROOT` | v1 素材受控收件目录；只允许普通文件，不能与 vault 重叠 |
| `AGENT_ASSET_VAULT_ROOT` | API 写、worker 读的持久 SHA-256 内容仓库；两进程必须挂载同一目录 |
| `AGENT_ASSET_MAX_BYTES` | 单个素材流式入库上限；默认 512 MiB，按平台与容量策略下调 |
| `AGENT_ASSET_MAX_TOTAL_BYTES` | 单份 v1 内容快照的素材总量上限；默认 2 GiB，不得小于单文件上限 |
| `AGENT_METRICS_COLLECTION_TIMEOUT_SECONDS` | v1 手动指标回采超时；默认 120 秒 |
| `AGENT_EXTERNAL_OPERATION_LEASE_SECONDS` | 外部读取幂等 lease；默认 300 秒，必须大于指标回采超时 |
| `AUTO_PUBLISH_ENABLED` | 默认 `false`；只控制后台扫描，真账号 canary 完成后才显式打开 |
| `SCHEDULER_BACKEND` | 只能是 `apscheduler` |
| `SCHEDULER_TIMEZONE` | 健康检查/报表 cron 的业务时区，默认 `Asia/Shanghai` |
| `SCHEDULER_POLL_SECONDS` | 持久任务扫描周期，默认 15 秒 |
| `SCHEDULER_MAX_CONCURRENCY` | 同时执行的发布上限，默认 4；按机器/浏览器容量下调 |
| `JOB_RETRY_BASE_SECONDS` | 重试基础退避，默认 60 秒 |
| `JOB_EXECUTION_TIMEOUT_SECONDS` | 单次 Publisher hard timeout，默认 1800 秒 |
| `JOB_RUNNING_TIMEOUT_SECONDS` | 失联 `RUNNING` 的 fail-closed 阈值，默认 7200 秒，必须大于执行超时 |
| `LOG_FORMAT` | 本地可用 `text`，日志平台建议 `json` |

生成密钥：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
python -c "import secrets; print(secrets.token_urlsafe(32))"
ai-ops gen-principal-token  # 每个 v1 principal 单独生成；raw token 只交给该调用方
```

第一个输出用于 `FERNET_KEY`，第二个用于 `API_KEY`。手工放入部署密钥管理系统，不要把值写进
Dockerfile、Compose YAML、Shell 历史或 Git。

Agent 调用应只使用 [Agent contract v1](agent-contract.md) 的 Bearer principal，不得持有 legacy
`API_KEY`。后者可以调用显式副作用管理端点；生产中必须非空、独立生成，并只经 TLS 和操作员访问
边界使用。`approval:read` / `approval:decide` 只能授予独立的 human principal，且应用会拒绝请求者/
计划创建者自签；审批客户端必须先读取脱敏快照，再把实际审阅的 `plan_digest` 作为
`expected_plan_digest` 回传。部署者仍必须把 human token 放在 Agent 无法读取的 SSO、审批系统或
硬件密钥边界。注意 `approval:read` 不只是元数据权限：它也能下载该审批绑定的原始素材字节，必须
按敏感内容读取权限分发、轮换和审计。

`AGENT_ASSET_IMPORT_ROOT` 是受控收件箱，不是任意宿主路径上传能力；让上游用独立系统账号写入，
不要授予其 vault 写权限。两个根目录必须分离且不能互相包含。API 会拒绝越界、symlink、设备和
超限文件，把字节原子复制到内容寻址 vault；计划保存不可变快照，并校验单文件与快照总量上限。
安全规范化的存储后缀作为独立元数据进入 `content_digest`；排程和 worker 会重算路径、大小与 SHA-256。
`AGENT_ASSET_VAULT_ROOT` 必须与数据库一样持久化，并由 API/worker 共享；备份和恢复时
保持两者一致。知乎/YouTube/SAU 和浏览器的磁盘 profile 还需用专用系统用户、`0700/0600` 权限和
受控备份单独保护。当前 v1 素材导入要求 POSIX `dir_fd`、`O_NOFOLLOW` 和安全 link/unlink 原语；
缺少这些能力的平台会 fail closed，不能降级成路径字符串检查。Windows 可运行其他控制面功能，
但在提供等价安全实现前不能承载带素材的 v1 exact 工作流。

v1 exact 计划当前只接受显式启用、且已绑定 `whoami.id` 稳定公开身份的知乎 CLI（`0.2.4`）
渲染器。计划/审批包绑定 Publisher kind、renderer identity、contract/adapter version、目标平台
账号身份、无宿主路径的 payload projection 与摘要；worker 只能调用该 Publisher，执行写入前会
再次读取并比对账号身份与 payload，不允许 fallback。知乎投影依赖精确锁定的
`markdown==3.10.3`。YouTube CLI（`v1.25.5`）因缺少可审计的只读频道身份探针，仅保留 legacy
canary，不进入 v1 exact。其他平台、未启用的 CLI、不支持的内容形状或版本/身份/payload 漂移均
fail closed；部署时不要把浏览器/SAU fallback 当成 v1 保底路径。这是执行一致性契约，不是 Stable
平台证据。

人工审批工作站先运行 `get-approval`，再按审阅包里的 `asset_id` 使用
`download-approval-asset --output <new-file>` 取回精确字节。客户端拒绝覆盖已有文件，并校验响应的
长度和 SHA-256 后原子落盘。POSIX 上输出目录必须已存在、归当前用户所有，且不得对 group/world
可写；输出文件必须是新路径。服务端在已验证 SHA-256 的同一文件句柄上直接流式返回，不重新打开
vault 路径，并且拒绝 `Range` 请求（HTTP 416）。反向代理必须允许该只读响应流式传输、不自行开启分段/
缓存转码，遵守应用返回的 `no-store`，且
不得记录 Bearer token、下载内容或宿主 vault 路径；下载上限和超时应与部署容量策略一致。

## 源码部署

```bash
git clone https://github.com/PeterGuy326/ai-ops-auto.git /opt/ai-ops-auto
cd /opt/ai-ops-auto

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[postgres]"  # SQLite 可用 pip install -e .
```

把密钥管理系统注入的环境变量提供给两个进程，然后在**单一管理步骤**中安全初始化/迁移：

```bash
ai-ops init-db
alembic current
```

该命令内部使用 Alembic；未知的无版本业务库会直接拒绝，不会猜测版本或覆盖 schema。

分别用 systemd/supervisor 等进程管理器启动：

```bash
ai-ops serve --host 127.0.0.1 --port 8000
ai-ops worker
```

反向代理负责 TLS、请求大小上限、超时和与公网的隔离。不建议直接向公网绑定 `0.0.0.0:8000`。

## Docker

根目录 Dockerfile 包含 Python API/worker、Alembic 迁移与服务端 HTML 模板，但**不包含**：

- React `frontend/dist`。
- Playwright/Camoufox 浏览器二进制。
- social-auto-upload、MoneyPrinterTurbo、FunClip 等外部仓库。
- 真实平台凭证。

因此基础镜像可以运行控制面和本地 SQLite 演示数据路径；当前还没有完整 Fake Publisher 闭环。
真平台 Publisher 需要额外、经过验证的运行镜像。

```bash
docker build -t ai-ops-auto:local .
```

Compose 最小拓扑示例：

```yaml
services:
  api:
    image: ai-ops-auto:local
    env_file: [.env]
    ports: ["127.0.0.1:8000:8000"]
    volumes: ["ai_ops_data:/app/data"]
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8000/health"]
      interval: 30s
      timeout: 5s
      retries: 5

  worker:
    image: ai-ops-auto:local
    command: ["ai-ops", "worker"]
    env_file: [.env]
    environment:
      SKIP_MIGRATIONS: "1"
    volumes: ["ai_ops_data:/app/data"]
    depends_on:
      api:
        condition: service_healthy

volumes:
  ai_ops_data:
```

API 容器的 entrypoint 执行 `ai-ops init-db` 安全迁移；worker 在 API 就绪后启动并显式跳过重复迁移。
在 Kubernetes 中应当改为单独 migration Job/initContainer，两个业务容器均设置 `SKIP_MIGRATIONS=1`。
当前不建议部署多副本。

## 启动验收

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS -H "X-API-Key: <redacted>" http://127.0.0.1:8000/topics
```

另外检查：

- API 和 worker 都只记录脱敏的数据库信息，不出现完整 `DATABASE_URL`。
- worker 日志明确输出 `AUTO_PUBLISH_ENABLED` 状态。
- `false` 时已到期 PublishJob 没有被自动执行。
- `false` 时 `/jobs/{id}/run` 等显式管理端点仍可产生副作用；验收时同时检查管理端访问边界。
- 通知目标和外部端点只来自当前部署配置，不是代码默认值。
- 开启自动发布前检查待执行 backlog；关闭期间积压的到期任务在开关打开后可能很快被扫描到。

## 打开真实发布

先完成下列门禁：

1. 平台在 [能力矩阵](platform-capabilities.md) 中不是 Stub。
2. 使用专用测试账号，记录适配器与上游精确版本。
3. 人工审核内容、目标账号、时间和平台条款。
4. 单账号、单平台、最小配额跑 canary，确认 post id/url 或等价服务端证据。
5. 备份数据库，配置告警，再设置 `AUTO_PUBLISH_ENABLED=true` 并重启 worker。

不要用“发布失败后立即再发一条覆盖”作为安全策略。这可能创建第二条公开内容；
应先停止 worker、确认平台端真实状态，再由人决定后续。

## 升级

1. 记录当前 commit/镜像 digest，备份数据库、Agent asset vault 和 `FERNET_KEY`。
2. 停止 worker，避免升级期间开始新的外部副作用。
3. 审查待应用 Alembic migration，在备份/预发数据上验证。
4. 安装新代码或拉取已验证镜像，执行一次 `ai-ops init-db`。
5. 启动 API 并验证读写，再启动 worker。
6. 保持自动发布关闭，直到新版本 canary 完成。

## 回滚

不要在没有审查 migration 的情况下盲目执行 `alembic downgrade -1`；downgrade 可能丢字段/数据。
首选恢复经过验证的数据库备份与对应代码/镜像。回滚前先停 worker，回滚后保持
`AUTO_PUBLISH_ENABLED=false` 并重新 canary。

## 已知限制

- 没有已验证的高可用/多区域部署。
- 没有 worker leader lease；单 worker 是部署约束，不是代码层选主。
- 硬退出时已 claim 的 `RUNNING` 任务会在失联阈值后 fail-closed 到失败状态，不会自动重发。
  先核对平台端是否已经成功，再人工处置，避免重复发布。
- 发布后的指标 callback 目前不是持久化任务，worker 重启可能丢失 1h/24h/7d 回采计划。
- worker 有统一执行超时，但部分 subprocess adapter 尚未保证取消时终止其子进程；生产环境仍需
  进程级监控和告警保护。
- 大多数平台 Publisher 不是 Stable，平台 UI 改版会使 selector 失效。
- 基础 Docker 镜像不能单独完成真实浏览器发布。
- 不是所有 Publisher 都返回 post identity 或真实 metrics，数据回流不应视为全平台闭环。
- 发布是不可逆外部副作用，运营者对账号、内容和平台合规负责。
