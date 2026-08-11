# Roadmap

## North Star

让任何强 Agent 都能**安全、持久、可审计**地管理中国内容渠道，同时把不可逆操作的授权留给人。

项目不以“自己再造一个 Agent”为目标，也不用平台数量作为主要成功指标。Agent contract v1
已经用独立 principal、最小 scope 和不可自签的精确计划审批区分 Agent 与 human approver；
旧 `API_KEY` 仍是兼容管理面，不能交给 Agent。`human` 身份能否代表真人，最终依赖部署者把该
凭证托管在 Agent 无法访问的组织权限边界。

## Phase 0 — Trustworthy alpha（已完成，2026-08-11）

目标：任务不丢、不重复发布、不默认误发，并且公开仓库的能力声明可以被验证。

- 持久化到期任务扫描、启动恢复、原子 claim、执行超时与失联任务失败关闭。
- 有界重试、指数退避、死信状态和可观测的失败原因。
- 单一、明确的 APScheduler 实现；在真正实现前不暴露 Celery 配置承诺。
- API/UI 鉴权与 CSRF 边界，反向代理部署安全。
- 后台自动发布默认关闭；只有后台扫描需要显式运营开关，显式管理执行端点仍可能产生真实副作用。
- 开源许可证、安全报告渠道、贡献流程、CI、发布记录和可安装资源。
- 保守的 Stable/Beta/Experimental/Stub 平台矩阵。
- CLI Adapter 的版本门禁、结果不确定语义和迁移矩阵；先接有 post identity 的后端。

退出条件：

- 重启后到期任务会恢复；并发 worker 只有一个能 claim 同一 job。
- 取消、超时或硬退出不会让任务永久停在 `RUNNING`，也不会对结果未知的发布盲目重试。
- 重试不需要人工重新排程，超限进入明确的终态。
- 新安装的默认路径不会真发布或向固定外部目的地发消息。
- 后端测试/lint、前端 lint/build、wheel 安装后迁移、PostgreSQL 空库迁移和 Docker
  build/runtime smoke 在 CI 通过。

验收记录：2026-08-11，合并后的 `main@16eccb5` 上六个 CI jobs 全部通过，覆盖 Python
3.11/3.12 后端、前端 lint/build、安装后 wheel 与 Alembic、PostgreSQL 空库迁移、Docker
build/runtime smoke。真实平台 canary 和独立人工审批身份不包含在 Phase 0 完成判断中。

## Phase 1 — Five-minute value（进行中）

目标：新用户在无凭证、无真平台的环境中看到完整价值链。

```text
ingest -> review -> dry-run plan -> durable job -> fake publish -> fake metrics -> review
```

已交付的 doctor/demo tranche：

- `ai-ops doctor`：检查数据库、资源、调度器、浏览器和外部适配器。
- `ai-ops demo`：使用隔离 SQLite、Fake Publisher/Fake Metrics 生成可重复的完整状态流；
  强制输出 synthetic/offline 标识，不使用凭证，外部调用数为 0。
- 人类可读与 JSON 输出、明确 dry-run 阶段、安装后 wheel 契约 smoke。
- 旧 `scripts/seed_demo.sh` 降级为 legacy UI seed，不再代表价值闭环。
- 独立 Bearer 调用者、最小作用域/RBAC、精确发布计划摘要和不可由请求者/Agent 自签的审批；
  legacy 管理 key 与 Agent 身份面保持分离。

待交付：

- 更完整的首次启动向导与运行时能力探测。
- 把**首个 Stable 中国平台**作为本阶段发布主目标：优先验证知乎文章，其次验证 B 站视频；
  必须先获得结构化 post identity/readback，并连续 30 天跑专用账号 canary，未达标继续标 Experimental。
- 将写入结果拆成 `accepted`（执行后端接受）、`deployed`（平台部署完成）、`verified`
  （目标 URL/平台后台可回查）；GitHub source branch SHA 目前只能到 `accepted`。
- 其他平台保持保守标记，不为平台数量降低证据门槛。

成功指标：新用户首次 demo 中位时间、安装成功率、首个待审内容建立率。

## Phase 2 — Agent-native contract（进行中）

目标：让 Agent 调用稳定契约，不需要理解平台 selector 或数据库细节。

已交付 v1 纵切：

- `stage_content`
- `plan_publication`
- `request_approval`
- 独立 human `get_approval`
- 独立 human `download_approval_asset`
- 独立 human `decide_approval`
- `schedule`
- `get_job_status`
- `collect_metrics`
- `review_performance`

交付形态包括严格 Pydantic DTO、稳定 Python service/HTTP client、`/v1` HTTP 路由和只通过 HTTP
工作的 CLI。修改操作使用持久幂等账本；human reviewer 能以受控二进制流下载并独立校验审批绑定
素材，审批同时绑定内容/素材、精确账号/平台与 UTC 排期；排程前重新计算摘要并用数据库唯一约束
防止重复扇出。

精确执行边界也已纳入 v1：每个 target 绑定 Publisher kind、renderer identity、contract/adapter
version、无宿主路径的 payload projection 和摘要，worker 执行前重算并禁止 fallback。当前只有
知乎 CLI `0.2.4` 实现 exact renderer，且需显式启用并配置由 `whoami.id` 规范化得到的稳定公开账号
身份；该身份进入审批展示和 plan digest，写前再次校验。YouTube CLI `v1.25.5` 因缺少可审计的
只读频道身份探针只保留 legacy canary；其他平台在 v1 计划阶段 fail closed。审批素材的存储后缀进入内容摘要，单文件/快照总量门禁、同一已验证文件
句柄的下载流和 Range 拒绝完成了素材审阅边界。这些是契约一致性证据，不代表平台已达 Stable。

发布后的反馈意图也已进入数据库：成功 job 固定创建 1h/24h/7d 三个 `MetricsCollectionTask`，
worker 用带过期时间和 fencing token 的 lease、有界重试/并发及窗口截止时间恢复执行。每个任务
唯一绑定一份 `scheduled` 快照；新的 24h task 当次健康判断按 `window_seconds=86400` 和绑定
metric 精确取数，不让手动采集或较晚的 7d 快照替代本次证据。缺少 post identity 或真实 collector 仍会明确失败，
不会因账本存在而伪造平台数据。

待交付：MCP 适配、审批 SSO/组织身份集成，以及随使用数据建立的
调用成功率/状态可解释性基线。

控制面对 Agent 的交付形式优先级：稳定 Python/HTTP 契约 → CLI → MCP。平台执行层则优先选择
有版本、结构化 post identity 和可回滚路径的官方 API/CLI。两层不要混为一谈，Agent 不能通过
任何一层越过人工审批策略。要兑现这一目标，必须先增加独立的调用者身份、作用域/RBAC 和可验证的
审批主体；v1 已提供应用内身份与 scope，生产仍应由外部网关/SSO 托管 human 凭证，并将 legacy
管理 key 与 Agent 隔离。详见 [Agent contract v1](agent-contract.md) 与
[CLI Adapter 选型](cli-adapters.md)。

成功指标：Agent 调用成功率、审批绕过事故数、任务状态可解释率。

## Phase 3 — Evidence and feedback moat（进行中）

已交付的 SDK tranche：

- Publisher Plugin SDK v1、版本化 entry-point group 与适配器兼容性 manifest。
- 精确 distribution/entry-point allowlist、metadata-only inventory 和显式插件 doctor。
- 插件 namespaced kind 已贯通 legacy/metrics/Agent exact renderer；每次 factory 构造重新校验，
  冲突或错误 fail closed。
- installed-wheel fixture 证明未启用插件不 import、显式 doctor 才加载授权代码。

待交付：

- 平台 canary、最后验证日期、上游版本与失效报警。
- 跨平台 Metrics 归一化、数据来源和质量标记。
- 独立的健康反馈 outbox 与经 canary 校准的冻结基线，避免评估器故障重复读取平台，
  也避免连续低值把自身基线逐步拉低。
- 内容变体/实验追踪，把指标可解释地回流给 Agent。
- 将 `jobhunt` 等非 Creator Ops 垂直能力迁出 core，改为独立插件或示例。

成功指标：真实发布成功率、重复发布事故数、指标可用率、适配器修复时间。

## 明确不做

- 不自己重写平台签名、反爬或视频编码引擎。
- 不用“支持 N 个平台”代替真实 E2E 证据。
- 不默认让 Agent 自主执行不可逆发布。
- 不把个人内网端点、绝对路径或通知目标带入开源默认配置。
