# MCP Agent bridge

`ai-ops-auto` 提供一个本地 **stdio MCP bridge**，把现有 Agent contract v1 放进 Codex、
Claude 等 MCP client 的工具列表。它不是新的 Agent，也不是第二套业务 API：bridge 只通过
`AgentContractClient` 调用正在运行的 `/v1` HTTP 控制面，数据库、审批、幂等账本、任务和
Publisher 的真相仍由控制面持有。

```text
Codex / Claude
      |
      | MCP over stdio
      v
 ai-ops-mcp (stateless bridge)
      |
      | AgentContractClient / Bearer / HTTP
      v
 ai-ops serve (/v1)
      |
      +------> database
      |
      +------> ai-ops worker ------> Publisher / metrics collector
```

当前是本地 stdio 入口，不是远程、托管或多租户 MCP 服务。stdio 进程不直接打开数据库、不启动
worker，也不调用 Publisher；它的重启不会改变持久任务状态。

## 工具范围

MCP 只暴露下面 7 个 Agent 操作，名称与 Agent contract v1 保持一致：

| MCP tool | 所需 scope | 作用与副作用边界 |
|---|---|---|
| `stage_content` | `content:stage` | 把内容暂存到控制面；写入操作，需要显式幂等键 |
| `plan_publication` | `plan:create` | 绑定精确内容、账号、执行 renderer 和 UTC 时间；写入操作，需要显式幂等键 |
| `request_approval` | `approval:request` | 创建独立人工审批请求；写入操作，需要显式幂等键 |
| `schedule` | `schedule:create` | 为已审批计划创建持久 job；写入操作，需要显式幂等键 |
| `get_job_status` | `job:read` | 读取一个持久 job 的状态 |
| `collect_metrics` | `metrics:collect` | 显式访问对应平台 collector 并写入归一化快照；需要显式幂等键 |
| `review_performance` | `performance:read` | 读取已持久化的表现复盘 |

以下 3 个 human-only 操作**不会出现在 MCP tools/list**：

- `get_approval`
- `download_approval_asset`
- `decide_approval`

它们继续使用独立 human principal 的 HTTP/CLI 环境。不要把 human Bearer token 配进 Codex，
也不要给 MCP bridge 配置 legacy 管理 `API_KEY`。MCP 不提供绕过人工审批的捷径：
`schedule` 仍会重算摘要并验证独立 human decision。

参数继续使用 v1 DTO，而不是一套 MCP 专用模型：

```json
{
  "request": {"schema_version": 1, "content_id": 17, "account_ids": [3]},
  "idempotency_key": "plan-20260812-001"
}
```

上例代表 mutation 的通用 envelope；具体 `request` 字段由对应 v1 request DTO 定义。
`get_job_status` 使用 `{"job_id": 42}`，`review_performance` 使用
`{"request": {"schema_version": 1, "job_ids": [42]}}`。成功结果是对应 v1 response DTO；失败结果
以 `isError` 标记并保留稳定 structured content：

```json
{
  "schema_version": 1,
  "error": {"code": "plan_not_approved", "message": "The publication plan has not been approved"}
}
```

bridge 不返回 HTTP 原始错误正文、Bearer token 或 adapter 任意异常。

## 启动条件

先按 [Getting Started](getting-started.md) 初始化并启动 API：

```bash
ai-ops init-db
ai-ops serve
```

为 Agent 生成独立 token，并在 `AGENT_PRINCIPALS` 中只保存其 SHA-256 verifier：

```bash
ai-ops gen-principal-token
```

具体 principal 配置、scope 和 human token 隔离要求见
[Agent contract v1](agent-contract.md#principals-and-scopes)。MCP bridge 使用下面两个环境变量：

| 变量 | 用途 |
|---|---|
| `AI_OPS_URL` | 控制面 origin，例如 `http://127.0.0.1:8000` |
| `AI_OPS_TOKEN` | 仅含所需 scope 的 Agent Bearer token |

`ai-ops-mcp` 在 stdin/stdout 上运行 MCP 协议，通常应由 MCP client 拉起，不需要单独驻留：

```bash
AI_OPS_URL=http://127.0.0.1:8000 \
AI_OPS_TOKEN='<agent bearer token>' \
ai-ops-mcp
```

stdout 专用于协议帧，诊断只写 stderr。正常工具调用错误只返回稳定、脱敏的错误信封，不返回 Bearer
token、HTTP 原始正文、Publisher `raw_response` 或异常 traceback；启动级故障仍应按 stderr 日志处理。

## 接入 Codex

先用本机秘密管理器把 `AI_OPS_URL` 和 `AI_OPS_TOKEN` 注入**启动 Codex 的环境**，再在用户级
`~/.codex/config.toml` 中只声明需要继承的变量名：

```toml
[mcp_servers."ai-ops-auto"]
command = "ai-ops-mcp"
env_vars = ["AI_OPS_URL", "AI_OPS_TOKEN"]
enabled_tools = [
  "stage_content",
  "plan_publication",
  "request_approval",
  "schedule",
  "get_job_status",
  "collect_metrics",
  "review_performance",
]
default_tools_approval_mode = "writes"
```

`env_vars` 只转发变量名，不把 token 字面值写入 Codex 配置；`enabled_tools` 固定允许列表，
`default_tools_approval_mode = "writes"` 让带写语义的工具默认要求 Codex 本地调用确认。这只是额外一
层 client 确认，不替代 v1 独立 human principal 的业务审批；控制面仍会 fail closed。不要把真实
token 写入 Codex 配置、shell 脚本、截图或日志，也不要把 token 作为 MCP tool 参数交给模型。若本机
或组织策略更严格，以更严格的审批和秘密注入规则为准。

配置后应能看到且只看到上述 7 个 `ai-ops-auto` 工具。若 client 启动失败，先直接确认：

```bash
curl -fsS http://127.0.0.1:8000/health
ai-ops agent get-job-status 1
```

第二条命令还需要同一终端已设置 `AI_OPS_URL` 和 Agent `AI_OPS_TOKEN`；job 不存在时返回稳定的
领域错误也能证明鉴权和 HTTP 路径已经连通。

## 执行与证据边界

- MCP 写工具不会在 transport 层盲目重试。调用者应提供 8–128 字符的显式幂等键；同 principal、
  operation、key 和相同请求会复用账本结果，不同请求会冲突失败。
- `schedule` 只创建数据库中的 `PENDING` job，不直接发布。只有独立 `ai-ops worker` 正在运行且
  `AUTO_PUBLISH_ENABLED=true` 时，后台扫描才会执行到期发布；默认仍是 `false`。
- `collect_metrics` 是显式外部读取，并可能写入指标快照。`AUTO_PUBLISH_ENABLED=false` 不会把它
  变成离线操作。
- MCP 返回现有 v1 DTO 和稳定领域错误，不暴露凭证、宿主路径或适配器任意响应。平台返回结果未知时，
  仍要求人工 reconciliation。
- MCP initialize、tools/list 和 mock HTTP 测试只证明 Agent 接入契约，不证明任何真实平台发布、
  readback、指标采集或 Stable 成熟度。平台能力仍以
  [平台能力矩阵](platform-capabilities.md)和专用账号 canary 为准。

## 当前非目标

- 远程 Streamable HTTP/SSE MCP、托管 MCP 和多租户会话。
- MCP Resources、Prompts、Agent memory 或自主执行循环。
- 通过 MCP 暴露 legacy 管理端点或 human 审批凭证。
- 把小红书等平台侧社区 MCP 直接 import 进控制面。
- 因 MCP 接入成功而提升任何 Publisher 的平台成熟度。
