# ai-ops-auto

**Agent-native, self-hosted, China-first Creator Ops Control Plane.**

`ai-ops-auto` 不是另一个通用 AI 智能体。它让 Codex、Claude、OpenClaw 或你自己的
Agent 能在一个有持久状态、审核状态流、账号策略和执行留痕的运行面上管理内容运营。

> 当前状态：**Alpha**。控制面、数据模型和适配器已有实现，但目前没有任何平台被标记为
> Stable。真实平台能力以 [平台能力矩阵](docs/platform-capabilities.md) 为唯一信源。

## 既然已经有 Codex，为什么还需要它？

Codex 是大脑，不应该同时充当你的长期任务数据库、发布审批系统和账号策略引擎。

| 层 | 职责 |
|---|---|
| Codex / 其他 Agent | 选题、生成、计划、复盘和异常诊断 |
| `ai-ops-auto` | 保存内容与任务状态，维护审核状态、限流、查重、排程、重试和留痕 |
| Publisher / 外部工具 | 把标准化内容翻译成具体平台操作 |
| 人 | 通过组织权限边界授权不可逆操作，处理登录、风控与最终责任 |

当前 Alpha 的管理 API 共用一个 `API_KEY`，因此项目自身不能区分调用者是 Agent 还是人；
`approve` 是可审计的状态迁移，不是强制的 human gate。要把发布批准真正留给人，需要在项目外
保管管理 key，或由反向代理/工作流系统增加身份、角色和审批策略。

偶尔发一条内容时，直接让 Codex 调一个上传脚本就够了。当任务变成多平台、多账号、
定时发布、审核、失败恢复和指标回流时，这个项目才有价值。

## 现在已有什么

- Topic → Article / Asset → Review → PublishJob → Metrics 的领域模型。
- 内容入库、审核状态流、按账号分发、历史回填和服务端运营界面。
- 数据库凭证字段的 Fernet 加密、账号健康、限流、内容查重与 Publisher Registry / fallback；
  外部 CLI/浏览器的 profile、cookie 和 OAuth 文件仍由部署机文件权限保护。
- API 与唯一 APScheduler worker 分进程，以持久化 `PublishJob` 为任务真相；当前**不支持 Celery**。
- social-auto-upload、Playwright 平台适配器、MoneyPrinterTurbo、FunClip 等外部工具接口。
- 单元测试与 mock 集成证据。这些不等于当前平台 UI 上的真实发布证据。

目前还不是：分布式队列、无人值守 SaaS、官方平台 API 聚合层，或已经完成的 Agent MCP 产品。

## 五分钟本地启动（不是完整价值演示）

下面的路径只使用本地 SQLite 和本机 HTTP，不需要 LLM key、平台 cookie 或外部工具。

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
ai-ops gen-fernet-key
# 将输出手工填入 .env 的 FERNET_KEY；保持 AUTO_PUBLISH_ENABLED=false

ai-ops init-db
ai-ops serve
```

在第二个终端启动唯一调度 worker（默认不真发布）：

```bash
source .venv/bin/activate
ai-ops worker
```

在第三个终端验证本地控制面：

```bash
curl -fsS http://127.0.0.1:8000/health
bash scripts/seed_demo.sh
```

打开 <http://127.0.0.1:8000/ui>。种子数据只写本地数据库；
`AUTO_PUBLISH_ENABLED=false` 会阻止后台扫描器自动真发布。

这条路径验证安装、控制面和持久任务基础设施，不包含 Fake Publisher/Fake Metrics 的完整闭环；
后者是 Roadmap Phase 1 的 `ai-ops demo` 交付。

完整步骤、前端开发和真平台启用前检查见 [Getting Started](docs/getting-started.md)。

## 安全默认

- `AUTO_PUBLISH_ENABLED=false`：后台调度不会自动真发布。
- 该开关不禁用显式管理操作；持有管理 `API_KEY` 的调用方仍可调用 `/jobs/{id}/run` 等有副作用端点。
- 当前只有一个全权限管理 key，不能用它证明“审批者一定是人”；需要在外层做密钥隔离或 RBAC。
- GitHub Pages 默认 `GITHUB_PAGES_DRY_RUN=true`。
- 知乎/YouTube CLI canary 与百家号/搜狐号 Stub 默认不进入真实写主链路。
- 开源配置不包含内网端点、个人路径、固定通知群或任何凭证。
- 对外网络暴露 API/UI 前必须配置 `API_KEY`，并在反向代理层启用 TLS。
- 真实发布是外部副作用；先用测试账号、最小配额和单平台验证。

安全问题请按 [SECURITY.md](SECURITY.md) 私密报告，不要在公开 Issue 中粘贴 cookie、token 或日志中的凭证。

## 架构边界

```text
Codex / Claude / OpenClaw / custom agent
                    |
              CLI / API / future MCP
                    v
        ai-ops-auto control plane
   content state | approval | policy | jobs | audit
                    |
             versioned adapters
                    v
       platform APIs / browser tools / SAU
                    |
                 metrics
                    +------> agent review
```

项目自己不重写平台签名、反爬或视频编码引擎。详见 [架构设计](docs/architecture.md) 和
[外部工具边界](docs/external-tools.md)。平台 CLI 的逐项审计和迁移次序见
[CLI Adapter 选型](docs/cli-adapters.md)。

## 项目方向

1. **Trustworthy alpha**：原子 claim、持久恢复、失败关闭、安全鉴权与开源治理已有代码，合并后仍需 CI/容器/PostgreSQL 验收。
2. **Five-minute value**：提供 `doctor` / `demo` / Fake Publisher，让新用户无凭证看到完整状态流；同时优先打通第一个 Stable 中国平台。
3. **Agent-native contract**：稳定 CLI/API/MCP 的入库、审批、排程、状态和复盘契约；平台侧
   优先复用能返回 post identity 的版本化 CLI/API，浏览器自动化作为受控 fallback。
4. **Evidence moat**：版本化 Adapter、平台 canary、跨平台指标归一化和实验追踪。

详细里程碑与成功指标见 [Roadmap](docs/roadmap.md)。

## 开发与贡献

```bash
bash scripts/verify.sh
```

`verify.sh` 会运行可用的语法、导入、测试和 lint 检查，并把未安装的可选外部工具列为警告。
贡献规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

[Apache License 2.0](LICENSE)
