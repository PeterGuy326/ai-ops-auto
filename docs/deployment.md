# 部署 SOP — ai-ops-auto 生产部署手册

> **底层逻辑**：从"能本地跑"到"能交生产"差的不是代码，是**运维 SOP 的颗粒度**。  
> 本文是部署的唯一信源，按顺序读 + 按顺序做。漏一步 = 上线 100% 出事故。

---

## 0. 阅读顺序

新接手运维 → 章节 1 → 2 → 3，按顺序逐条做。  
升级 → 章节 4。回滚 → 章节 5。容器化 → 章节 6。  
凭证轮换 → 章节 7。可观测性 → 章节 8。出问题 → 章节 9 FAQ。

---

## 1. 环境要求

- **Python**：3.11+（pyproject `requires-python = ">=3.11"`，3.10 直接装不上）
- **操作系统**：Linux / macOS（容器内推荐 `python:3.11-slim`）
- **数据库**：二选一
  - SQLite（默认，零配置，单进程小流量够用，**生产慎用**——并发写锁问题）
  - PostgreSQL 14+（生产推荐，装 `pip install -e .[postgres]` 引入 psycopg2-binary）
- **浏览器**（**只有真发布时需要**，纯 API 后端可跳过）：
  ```bash
  # playwright 内置浏览器（默认引擎）
  playwright install chromium
  # 高风控平台（小红书）走 camoufox 加固档：
  pip install -e .[stealth-pro]
  python -m camoufox fetch
  ```
- **磁盘**：素材产物 `data/assets`、视频产物 `data/outputs`、SQLite `data/ai_ops.db`，预留 ≥ 20GB

---

## 2. 环境变量清单

`.env` 文件放在项目根，与 `pyproject.toml` 同级。**永远不要 commit 到 git**（已在 .gitignore）。

### 2.1 必填（缺一启动就废）

| 变量 | 说明 | 示例 |
|------|------|------|
| `FERNET_KEY` | Cookies / 凭证对称加密密钥。**一旦设定不可换**，换了所有已加密 cookies 立即解密失败 | `bWVfb25seV9hX3Rlc3RfMzJfYnl0ZXNfMTIzNDU2Nzg=` |
| `DATABASE_URL` | SQLAlchemy URL；不设则用 `sqlite:///./data/ai_ops.db` | `postgresql+psycopg2://user:pw@host:5432/aiops` |

**生成 FERNET_KEY 命令**（**必须在首次部署前跑一次并把结果安全保存到密钥管理系统**）：
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
> 备份建议：FERNET_KEY 存到 HashiCorp Vault / AWS Secrets Manager / 1Password，**纸质备份一份保险柜**。丢了 = 所有账号 cookie 报废 → 全员重新扫码登录。

### 2.2 强烈建议（不设有安全风险）

| 变量 | 不设的代价 | 推荐值 |
|------|----------|--------|
| `API_KEY` | **dev 模式自动放行**——任何人 curl `/topics` 都能改你数据 | 32+ 字符随机串：`python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `FEISHU_WEBHOOK_URL` | 失败 / 风控告警没人收到，运维变盲人 | 飞书群机器人 webhook URL |

### 2.3 可观测性（接 Sentry / ELK 时必填）

| 变量 | 说明 | 默认 |
|------|------|------|
| `SENTRY_DSN` | Sentry 上报 DSN；空 = 不启用（sentry-sdk 软依赖） | `""` |
| `SENTRY_ENVIRONMENT` | Sentry 环境 tag | `dev` |
| `LOG_FORMAT` | `text`（本地友好）/ `json`（ELK/Datadog 推荐） | `text` |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` / `ERROR` | `INFO` |

### 2.4 业务可调（按需）

| 变量 | 说明 | 默认 |
|------|------|------|
| `BROWSER_ENGINE` | `playwright_chrome_channel` / `playwright_chromium` / `patchright` / `camoufox` | `playwright_chrome_channel` |
| `BROWSER_HEADLESS` | 高风控平台建议 `false` | `false` |
| `BROWSER_PROXY` | 每账号独立 IP 反风控核心 | 空 |
| `PUBLISH_MIN_INTERVAL_SECONDS` | 同账号两次发布最小间隔 | `14400`（4h）|
| `PUBLISH_MAX_PER_DAY` | 单账号每日上限 | `2` |
| `NURTURE_DAYS` | 新号养号期天数 | `7` |
| `LLM_DEFAULT` | `openai` / `anthropic` / `deepseek` / `dashscope` | `openai` |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `DEEPSEEK_API_KEY` / `DASHSCOPE_API_KEY` | 对应 LLM 厂商 key | 空 |
| `API_HOST` / `API_PORT` | uvicorn 监听 | `127.0.0.1:8000` |

完整字段对照 `src/ai_ops/config.py` 的 `Settings` 类。

### 2.5 .env 模板（首次部署照抄）

```dotenv
# === 必填 ===
FERNET_KEY=<上面命令生成的 key>
DATABASE_URL=sqlite:///./data/ai_ops.db

# === 强烈建议 ===
API_KEY=<上面命令生成的 token>
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx

# === 可观测性（生产推荐）===
SENTRY_DSN=
LOG_FORMAT=json
LOG_LEVEL=INFO

# === 业务可调 ===
BROWSER_ENGINE=playwright_chrome_channel
PUBLISH_MIN_INTERVAL_SECONDS=14400
PUBLISH_MAX_PER_DAY=2
```

---

## 3. 首次部署 SOP（**严格按顺序**）

### 3.1 准备代码 + 依赖

```bash
# 1. 拉代码
git clone <repo-url> /opt/ai-ops-auto
cd /opt/ai-ops-auto

# 2. 装依赖（生产档：不装 dev / llm-* 可选包）
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .

# 3. 装可选 stealth 档（如需高风控平台）
pip install -e .[stealth-pro]
python -m camoufox fetch
```

### 3.2 准备 .env + 密钥

```bash
# 1. 生成 FERNET_KEY 并安全保存（密码管理 + 离线备份）
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2. 生成 API_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"

# 3. 按 §2.5 模板写 .env
vim .env
chmod 600 .env  # 权限收紧
```

### 3.3 **跑 alembic upgrade head**（**第一关键步骤**）

```bash
# 创建 schema 到最新版本
alembic upgrade head

# 验证当前版本号
alembic current
```

> ⚠️ **绝对不要**用 `python scripts/init_db.py` 替代——那是 dev 路径（`Base.metadata.create_all`），生产用 = 后续加字段全部炸（无法平滑迁移）。`alembic upgrade head` 才是唯一正确路径。

### 3.4 启动服务

```bash
# 开发 / 单实例（够小流量验证）
uvicorn ai_ops.api.main:app --host 0.0.0.0 --port 8000

# 生产：gunicorn + uvicorn worker（worker 数推荐 = CPU × 2 + 1）
pip install gunicorn
gunicorn ai_ops.api.main:app \
  --workers $(($(nproc) * 2 + 1)) \
  --worker-class uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --access-logfile - \
  --error-logfile -
```

### 3.5 启动自检

```bash
# 健康检查（不需要 API_KEY）
curl -s http://127.0.0.1:8000/health
# 期望：{"ok":true}

# 鉴权确认（必须带 API_KEY）
curl -s -H "X-API-Key: <你的 API_KEY>" http://127.0.0.1:8000/topics
# 期望：JSON 数组（首次为空 []）
```

如果启动日志看到 `[DEPLOY-CHECK]` warning（API_KEY 未设 / FERNET_KEY 未设），说明 .env 配错——**回到 §2 重检环境变量**。

---

## 4. 升级 SOP

```bash
# 1. 拉新代码
cd /opt/ai-ops-auto
git fetch origin
git checkout <new-tag>

# 2. 装新依赖（可能有新增）
source .venv/bin/activate
pip install -e .

# 3. 跑迁移（必跑，不论代码有没有新 migration）
alembic upgrade head
alembic current  # 确认版本前进

# 4. 重启 service
systemctl restart ai-ops-auto
# 或 supervisor / pm2 / docker-compose restart api
```

> 升级前**强烈建议**：备份 SQLite `data/ai_ops.db` 或 Postgres `pg_dump`，回滚救命用。

---

## 5. 回滚 SOP

```bash
# 1. 回滚一步迁移（如新版本有 schema 变更）
alembic downgrade -1
alembic current  # 确认版本回退

# 2. 回退代码
git checkout <old-tag>

# 3. 重装依赖（如需要）
pip install -e .

# 4. 重启 service
systemctl restart ai-ops-auto
```

> 如果新版本**没有 schema 变更**（`alembic history` 看一致），可跳过 step 1 直接回代码。

---

## 6. 容器化部署（Docker / docker-compose）

项目根有 `Dockerfile` + `docker-entrypoint.sh`，entrypoint 会**自动跑 alembic upgrade head 再启 uvicorn**。

### 6.1 单容器

```bash
# 构建
docker build -t ai-ops-auto:latest .

# 运行（挂载 data 卷 + 注入环境变量）
docker run -d \
  --name ai-ops-auto \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  --env-file .env \
  ai-ops-auto:latest
```

### 6.2 docker-compose.yml（参考片段）

```yaml
version: "3.9"
services:
  api:
    build: .
    image: ai-ops-auto:latest
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - ./data:/app/data
    env_file:
      - .env
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8000/health"]
      interval: 30s
      timeout: 5s
      retries: 3
  # postgres: 可选——把 .env 的 DATABASE_URL 切到 postgres://...
  # postgres:
  #   image: postgres:16
  #   environment:
  #     POSTGRES_DB: aiops
  #     POSTGRES_USER: aiops
  #     POSTGRES_PASSWORD: <strong-pw>
  #   volumes:
  #     - ./pgdata:/var/lib/postgresql/data
```

### 6.3 K8s 注意点

- `data/` 必须挂 PersistentVolume（SQLite 文件 / 素材产物都靠它）
- entrypoint 自动跑 `alembic upgrade head` → 多 Pod 滚动更新时建议串行（initContainer 跑 migration 后 Deployment 起 Pod），避免多实例并发 migration race

---

## 7. 凭证轮换

| 凭证 | 能不能轮换 | 操作 |
|------|----------|------|
| `FERNET_KEY` | **不能轻易换** | 换了之后所有 Account.encrypted_credential 解密失败 → 全平台账号需重新扫码登录。**必须的话**先 dump 所有 cookies 明文 → 换 key → 用新 key 重新 encrypt 落库 |
| `API_KEY` | ✅ 可滚动轮换 | 更新 .env → 重启 service → 通知所有调用方更新 X-API-Key header |
| `OPENAI_API_KEY` 等 LLM key | ✅ 可轮换 | 更新 .env → 重启 |
| `FEISHU_WEBHOOK_URL` | ✅ 可换 | 更新 .env → 重启；旧 webhook 失效会 4xx 但不阻塞主流程 |
| `BROWSER_PROXY` | ✅ 可换 | 更新 .env → 重启；账号粒度的代理建议改 `Account.proxy` 字段（DB 层） |

---

## 8. 可观测性接入

### 8.1 Sentry

```bash
# 1. 装软依赖
pip install sentry-sdk

# 2. .env 配 DSN
echo "SENTRY_DSN=https://<key>@o<org>.ingest.sentry.io/<project>" >> .env
echo "SENTRY_ENVIRONMENT=prod" >> .env

# 3. 重启 → init_observability() lifespan 内会自动启用
```

### 8.2 Feishu webhook（失败 / 风控告警）

```bash
# 1. 飞书群 → 设置 → 群机器人 → 添加自定义机器人 → 复制 webhook URL
# 2. 配置
echo "FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxxxx" >> .env
# 3. 重启
```

去重窗口默认 5 分钟内最多 2 条同事件（`NOTIFY_DEDUP_WINDOW_SECONDS` + `NOTIFY_DEDUP_THRESHOLD`）。

### 8.3 结构化日志接 ELK / Loki / Datadog

```bash
# .env 切 json 格式
echo "LOG_FORMAT=json" >> .env
echo "LOG_LEVEL=INFO" >> .env

# stdout 直接是 JSON 行 → docker logs / journalctl 接 Fluent Bit / Vector 转发
```

---

## 9. 常见运维问题 FAQ

### Q1. 启动报 `sqlalchemy.exc.OperationalError: no such table: topics`

→ alembic 没跑。**马上跑** `alembic upgrade head`，再重启。

### Q2. `alembic current` 显示版本比代码 `alembic/versions/` 里最新的低？

→ 这就是 schema 落后状态，写数据会炸。**立刻**跑：
```bash
alembic upgrade head
alembic current  # 确认追平
```

### Q3. 启动后所有 publisher 调用都 `cryptography.fernet.InvalidToken`

→ `FERNET_KEY` 跟当年加密 cookies 时用的不一致。**不要硬换**：
- 排查 `.env` / 环境变量 / docker secrets 是不是覆盖了正确的 KEY
- 实在找不回旧 KEY → 所有账号必须重新扫码登录（DB 层 `Account.encrypted_credential` 全部置 NULL）

### Q4. 所有 API 请求 401 Unauthorized

→ `API_KEY` 已设但调用方没带 / 带错了。客户端必须发：
```
X-API-Key: <你 .env 里的 API_KEY 值>
```

### Q5. `/health` 通但写 API 报 401，并且 .env 里 API_KEY 没设

→ 这就是 dev 模式自动放行被生产配置遗忘的典型坑。**设 API_KEY 后重启**。

### Q6. 数据卷迁移（换机器 / 换容器）

```bash
# 老机器：
tar czf data-backup.tgz data/

# 新机器：
tar xzf data-backup.tgz
# .env 复制过去（FERNET_KEY 必须一致！）
# alembic upgrade head（追平 schema）
# 启动
```

### Q7. Postgres 切换（从 SQLite 迁移）

参考 `docs/dev-db-migration.md`（如有）。简版：dump SQLite → 建 Postgres schema (`alembic upgrade head` on PG URL) → 用 csv / pandas / 脚本搬数据。

### Q8. Cron / 定时任务（每日健康检查、日报 / 周报）没跑

→ APScheduler in-process，service 重启间隙会丢任务。多副本时只一个 worker 应注册 cron（用 leader election / `SCHEDULER_BACKEND=celery` 切 Celery+Redis）。

### Q9. K8s 滚动更新时 alembic 跑了多次？

→ initContainer 单独跑 migration，App container 不再跑（Dockerfile 留环境变量 `SKIP_MIGRATIONS=1` 可禁用 entrypoint 的 alembic）。

---

## 10. 验收 checklist（上线前最后一遍走）

- [ ] FERNET_KEY 已生成 + 安全备份
- [ ] API_KEY 已设非空
- [ ] `.env` 文件权限 600，**未** commit
- [ ] `alembic current` 输出 = `alembic heads`（schema 追平）
- [ ] `curl /health` 返回 200
- [ ] 带 X-API-Key 的 `curl /topics` 返回 JSON
- [ ] 飞书 webhook 收到测试消息（用 `curl` 手动 hit webhook）
- [ ] 启动日志无 `[DEPLOY-CHECK]` warning
- [ ] `data/` 目录已挂卷 / 已备份
- [ ] 监控（Sentry / 日志收集）接好

> 这 10 项过完才叫"部署完成"。少一项 = 你在生产埋雷。
