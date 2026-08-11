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

Agent contract v1 已把 Agent 与审批者拆成独立 Bearer principal，并用作用域、内容/目标/排期摘要、
幂等账本和数据库唯一约束强制“先计划、再由独立 human principal 审批、最后排程”。旧的
`X-API-Key` 路由仍是全权限管理兼容面，不能交给 Agent。`human` 是可验证的独立凭证身份；要证明
真人实际参与，仍需把该凭证托管在 Agent 无法访问的 SSO、审批系统或硬件密钥边界。

偶尔发一条内容时，直接让 Codex 调一个上传脚本就够了。当任务变成多平台、多账号、
定时发布、审核、失败恢复和指标回流时，这个项目才有价值。

## 现在已有什么

- Topic → Article / Asset → Review → PublishJob → Metrics 的领域模型。
- 内容入库、审核状态流、按账号分发、历史回填和服务端运营界面。
- 数据库凭证字段的 Fernet 加密、账号健康、限流、内容查重与 Publisher Registry / fallback；
  外部 CLI/浏览器的 profile、cookie 和 OAuth 文件仍由部署机文件权限保护。
- API 与唯一 APScheduler worker 分进程，以持久化 `PublishJob` 为任务真相；当前**不支持 Celery**。
- 版本化 Agent Python/HTTP/CLI 契约：内容暂存、精确发布计划、独立审批、幂等排程、任务状态、
  手动指标采集和结构化复盘。详见 [Agent contract v1](docs/agent-contract.md)。
- v1 计划把 Publisher kind、renderer identity/contract/adapter version 和无宿主路径的最终平台
  payload projection 及其摘要一起交给 human review；worker 执行前重算，不允许切换到另一
  Publisher。当前只有显式启用、且配置了稳定公开账号身份的知乎 CLI 具备 exact renderer；
  YouTube CLI 因缺少可审计的只读频道身份探针，仅保留 legacy canary。其他平台在 v1 计划阶段
  fail closed；这不代表知乎已经 Stable。
- Agent 素材只从显式 import root 入库，经大小门禁和 SHA-256 校验原子复制到持久 vault；发布计划保存
  不可变内容快照。独立 human 可通过 `approval:read` 下载并校验该审批绑定的精确素材字节，API 不暴露
  宿主路径；文件后缀和单文件/总量上限也在契约里受控。下载端在同一已验证文件句柄上流式
  返回、拒绝 Range；排程与 worker 会再次校验素材。
- social-auto-upload、Playwright 平台适配器、MoneyPrinterTurbo、FunClip 等外部工具接口。
- 单元测试与 mock 集成证据。这些不等于当前平台 UI 上的真实发布证据。

目前还不是：分布式队列、无人值守 SaaS、官方平台 API 聚合层，或已经完成的 Agent MCP 产品。

## 五分钟离线价值演示

先做只读环境诊断，再跑一条完整的合成链路：

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
ai-ops init-db
ai-ops doctor
ai-ops demo
```

如果在 `init-db` 前先运行 `doctor`，它会以只读 FAIL 报告未初始化的 SQLite，但不会
替你创建文件或修改数据。执行 `init-db` 后复检即可。

“SYNTHETIC — NO EXTERNAL ACTION” 是这条链路的强制边界。`ai-ops demo` 使用隔离的
SQLite、Fake Publisher 和 Fake Metrics，按顺序验证入库、审核、dry-run 计划、持久任务、
合成发布、合成指标和最终复核。它不需要、不使用、也不会发送平台凭证，不访问真实平台，外部调用数为 0；
默认的临时数据会在结束后清理。可用 `--json` 获取 Agent 可验证的结果。

若要继续运行 API/UI 和 worker，再补齐账号凭证加密密钥和 legacy 管理 API key：

```bash
ai-ops gen-fernet-key
python -c "import secrets; print(secrets.token_urlsafe(32))"
# 将两个输出分别手工填入 .env 的 FERNET_KEY 和 API_KEY；
# 保持 LEGACY_DEV_AUTH_BYPASS=false、AUTO_PUBLISH_ENABLED=false

ai-ops serve
```

在第二个终端启动唯一调度 worker（默认不真发布）：

```bash
ai-ops worker
```

在第三个终端验证本地控制面：

```bash
curl -fsS http://127.0.0.1:8000/health
API_KEY='<与 .env 相同的管理 key>' bash scripts/seed_demo.sh
```

打开 <http://127.0.0.1:8000/ui>。`seed_demo.sh` 只是旧的 UI 占位数据脚本，不是价值链路或
发布成功证据。种子数据只写本地数据库；
`AUTO_PUBLISH_ENABLED=false` 会阻止后台扫描器自动真发布。

完整步骤、前端开发和真平台启用前检查见 [Getting Started](docs/getting-started.md)。

## 安全默认

- `AUTO_PUBLISH_ENABLED=false`：后台调度不会自动真发布。
- 该开关不禁用显式管理操作；持有管理 `API_KEY` 的调用方仍可调用 `/jobs/{id}/run` 等有副作用端点。
- `/v1` 使用独立 Bearer principal 和最小 scope，永不继承空 `API_KEY` 的开发放行；
  `approval:read` / `approval:decide` 只能配置给 `type=human`，审批者必须回传实际审阅的
  `expected_plan_digest`，请求者/计划创建者不能自签。`approval:read` 同时允许读取审批包和下载其中的
  原始素材，必须按敏感数据权限隔离。
- v1 发布计划必须找到可生成稳定、无路径 payload projection 的 exact renderer；当前只允许
  知乎 CLI `0.2.4`，并把 `whoami.id` 的规范化公开身份纳入审批目标和 plan digest。无渲染器、
  账号身份、版本或 payload 摘要漂移、或素材规则不匹配均 fail closed，exact job 不走
  Publisher fallback。YouTube CLI `v1.25.5` 当前仅用于 legacy canary。
- 旧 `X-API-Key` 仍是全权限 break-glass/兼容入口，不能发给 Agent 或与 Bearer token 复用；生产及
  任何网络暴露环境必须设置独立强随机值，并限制在 TLS/操作员访问边界内。
- GitHub Pages 默认 `GITHUB_PAGES_DRY_RUN=true`。
- 知乎/YouTube CLI canary 与百家号/搜狐号 Stub 默认不进入真实写主链路。
- 开源配置不包含内网端点、个人路径、固定通知群或任何凭证。
- 对外网络暴露 API/UI 前还必须配置反向代理 TLS、请求限制和网络访问策略。
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

1. **Trustworthy alpha**：原子 claim、持久恢复、失败关闭、安全鉴权与开源治理已在
   2026-08-11 的 `main@16eccb5` 通过六个 CI jobs 验收。
2. **Five-minute value**：`doctor` / `demo` / Fake Publisher/Fake Metrics 的离线 tranche 已交付；
   独立身份、最小 scope 和不可自签审批已进入 v1 契约；首个 Stable 中国平台与
   `accepted/deployed/verified` 结果分层仍在进行中。
3. **Agent-native contract**：Python/HTTP/CLI 的十操作 v1 纵切和知乎 CLI 的目标账号/exact renderer
   绑定已落地，YouTube exact 等待只读频道身份探针；下一步补 MCP，并把发布后
   指标回采从内存回调升级为可恢复的数据库任务。平台侧继续优先复用能返回 post identity 的
   版本化 CLI/API，浏览器自动化作为受控 fallback。
4. **Evidence moat**：版本化 Adapter、平台 canary、跨平台指标归一化和实验追踪。

详细里程碑与成功指标见 [Roadmap](docs/roadmap.md)。

## 开发与贡献

```bash
bash scripts/verify.sh
```

`verify.sh` 把测试、lint、前端构建（仓库包含前端时）和 Python 打包作为必过门禁；未安装的
真实平台外部工具只列为警告。
贡献规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

[Apache License 2.0](LICENSE)
