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

## 2. 安全的本地配置

```bash
cp .env.example .env
ai-ops gen-fernet-key
```

把命令输出手工粘贴到 `.env` 的 `FERNET_KEY=` 后面。不要把 `.env` 或输出的 key 提交到 Git。

离线演示保持下列默认值：

```dotenv
DATABASE_URL=sqlite:///./data/ai_ops.db
API_HOST=127.0.0.1
AUTO_PUBLISH_ENABLED=false
GITHUB_PAGES_DRY_RUN=true
```

`AUTO_PUBLISH_ENABLED=false` 只会禁止后台扫描器自动真发布。显式的运行端点仍是有副作用的管理操作；
离线演示中不要调用它们。

## 3. 初始化与启动

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
- `seed_demo.sh` 创建本地 Topic 和 Article 记录。
- <http://127.0.0.1:8000/ui> 能看到服务端运营界面。
- 没有网页被提交到外部内容平台。

`seed_demo.sh` 面向全新的演示数据库，不建议对已有数据的库重复运行。

## 4. 离线 Prompt 演示

```bash
python scripts/demo_topic_prompt.py
```

该脚本用 Fake Driver 展示主题 prompt 组装，不发送网络请求，不生成真实平台内容。

## 5. React 前端（可选）

服务端 `/ui` 不需要 Node.js。如果要开发 React 运营台：

```bash
cd frontend
npm ci
npm run dev
```

Vite 会把 `/api` 代理到 `http://127.0.0.1:8000`。前端不会在 API 失败时伪造成功数据；
错误应当在界面或开发者工具中可见。

## 6. 自检

```bash
bash scripts/verify.sh
```

脚本对已安装的项目执行 Python 语法/导入、`pytest`、`ruff`、前端 lint/build 和打包检查。
可选平台工具未安装时是警告，核心检查失败时脚本返回非零。

## 7. 真实平台之前

1. 阅读 [平台能力矩阵](platform-capabilities.md)，不要把 Experimental/Stub 当作可用承诺。
2. 使用专用测试账号，完成平台条款、隐私与内容合规检查。
3. 配置非空 `API_KEY`，不向公网直接暴露 Uvicorn。当前它是全权限管理 key，不能区分 Agent 与人；
   强制人工审批需由外部网关/工作流隔离权限。
4. 一次只启用一个账号和一个平台，先由人核对；需要后台扫描时再显式设置
   `AUTO_PUBLISH_ENABLED=true`。显式 `/jobs/{id}/run` 不受该开关保护。
5. 记录适配器版本、上游 commit、操作系统、内容类型、时间和平台返回值。

部署拓扑和升级限制见 [部署指南](deployment.md)。
