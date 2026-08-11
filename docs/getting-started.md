# Getting Started

这份指南先跑一条**不访问 LLM、不登录平台、不真发布**的本地路径。它用来验证控制面，
不是平台发布 E2E 证据。

## 1. 环境

- Python 3.11 或 3.12
- Git
- 可选：Node.js 22（只有开发 React 前端时需要）

```bash
git clone https://github.com/PeterGuy326/ai-ops-auto.git
cd ai-ops-auto

python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Windows PowerShell 请用 `.venv\Scripts\Activate.ps1` 激活虚拟环境。平台浏览器适配器主要在
Linux/macOS 开发；Windows 平台 E2E 尚未建立维护证据。

## 2. 五分钟离线闭环

复制无凭证的安全默认配置，初始化本地 SQLite，然后先运行只读自检、再运行合成演示：

```bash
cp .env.example .env
ai-ops init-db
ai-ops doctor
ai-ops demo
```

`init-db` 是这条 happy path 中唯一个会创建控制面数据库的步骤。如果在它之前先运行
`doctor`，未初始化的 SQLite 会被只读地报告为 FAIL，且命令返回非零；`doctor` 不会隐式创建
数据库。运行 `init-db` 后重新检查即可。

`doctor` 检查数据库、打包资源、调度器配置、浏览器与可选适配器，不会启动浏览器、
执行登录或向内容平台发起请求。如果它报告核心阻塞项，会以非零码退出；可选浏览器/适配器
未安装只是警告。

`demo` 明确标记为 **SYNTHETIC / OFFLINE / NO EXTERNAL ACTION**：它在隔离的 SQLite 中跑完
下列链路，不需要、不使用、也不会发送 LLM key、cookie、OAuth 或平台账号，不访问真实平台，
`external_calls` 恒为 `0`：

```text
ingest -> review -> dry-run plan -> durable job -> fake publish -> fake metrics -> final review
```

默认演示库位于私有临时目录，完成后自动清理。若要保留结果供手工查看，目标文件
必须尚不存在：

```bash
ai-ops demo --database ./data/offline-demo.db
```

CLI 启动时基础 `.env` 仍需通过类型校验；先运行 `doctor` 可得到脱敏的配置错误。演示执行本身
使用独立数据库、固定本地策略和 Fake 后端，不读取生产内容库或凭证库。

Agent 和自动化可使用纯 JSON 契约：

```bash
ai-ops doctor --json
ai-ops demo --json
```

演示通过时，`review.passed` 为 `true`，同时可验证 `synthetic=true`、`offline=true`、
`credentials_used=false` 和 `external_calls=0`。这些只能证明控制面闭环，不是任何真实
平台的发布证据。

## 3. 安全的本地配置

```bash
ai-ops gen-fernet-key
```

若跳过了上一节，先运行 `cp .env.example .env`。把命令输出手工粘贴到 `.env` 的
`FERNET_KEY=` 后面。不要把 `.env` 或输出的 key 提交到 Git。

离线演示保持下列默认值：

```dotenv
DATABASE_URL=sqlite:///./data/ai_ops.db
API_HOST=127.0.0.1
AUTO_PUBLISH_ENABLED=false
GITHUB_PAGES_DRY_RUN=true
```

`AUTO_PUBLISH_ENABLED=false` 只会禁止后台扫描器自动真发布。显式的运行端点仍是有副作用的管理操作；
离线演示中不要调用它们。

## 4. 初始化与启动

```bash
ai-ops init-db
ai-ops serve
```

`ai-ops init-db` 是统一的安全 Alembic 路径：空库跑完整迁移，旧版本库升级；历史
`create_all` 库只有在 schema 与当前模型精确一致时才会 stamp head，其他无版本业务库会拒绝。
运行入口不会再用 `create_all` 补表。

在第二个终端启动唯一调度 owner：

```bash
source .venv/bin/activate
ai-ops worker
```

API 与 worker 必须分进程。在 `AUTO_PUBLISH_ENABLED=false` 时，worker 保持健康/报表 cron，
但不会自动执行发布任务。

在第三个终端检查：

```bash
source .venv/bin/activate
curl -fsS http://127.0.0.1:8000/health
bash scripts/seed_demo.sh
```

期望结果：

- `/health` 返回包含 `"ok": true` 的 JSON。
- `seed_demo.sh` 创建旧 UI 展示所需的本地 Topic 和 Article 占位记录。
- <http://127.0.0.1:8000/ui> 能看到服务端运营界面。
- 没有网页被提交到外部内容平台。

`seed_demo.sh` 是 legacy UI seed，不经过完整的审核、任务、合成发布和指标链路；它不是
`ai-ops demo` 的替代品，也不是真实发布证据。它面向全新的演示数据库，不建议对已有数据的库
重复运行。

## 5. 离线 Prompt 演示

```bash
python scripts/demo_topic_prompt.py
```

该脚本用 Fake Driver 展示主题 prompt 组装，不发送网络请求，不生成真实平台内容。

## 6. React 前端（可选）

服务端 `/ui` 不需要 Node.js。如果要开发 React 运营台：

```bash
cd frontend
npm ci
npm run dev
```

Vite 会把 `/api` 代理到 `http://127.0.0.1:8000`。前端不会在 API 失败时伪造成功数据；
错误应当在界面或开发者工具中可见。

## 7. 自检

```bash
bash scripts/verify.sh
```

脚本对已安装的项目执行 Python 语法/导入、`pytest`、`ruff`、前端 lint/build 和打包检查。
可选平台工具未安装时是警告，核心检查失败时脚本返回非零。

## 8. 真实平台之前

1. 阅读 [平台能力矩阵](platform-capabilities.md)，不要把 Experimental/Stub 当作可用承诺。
2. 使用专用测试账号，完成平台条款、隐私与内容合规检查。
3. 为 legacy 管理端点配置非空 `API_KEY`，不向公网直接暴露 Uvicorn；它是全权限 key，不能交给
   Agent。Agent 使用 v1 独立 Bearer scopes，human approver token 由外部网关/审批工作流隔离保管。
4. 一次只启用一个账号和一个平台，先由人核对；需要后台扫描时再显式设置
   `AUTO_PUBLISH_ENABLED=true`。显式 `/jobs/{id}/run` 不受该开关保护。
5. 记录适配器版本、上游 commit、操作系统、内容类型、时间和平台返回值。

部署拓扑和升级限制见 [部署指南](deployment.md)。
