# Roadmap

## North Star

让任何强 Agent 都能**安全、持久、可审计**地管理中国内容渠道，同时把不可逆操作的授权留给人。

项目不以“自己再造一个 Agent”为目标，也不用平台数量作为主要成功指标。
“授权留给人”是产品目标，不是当前 Alpha 已经强制实现的安全属性：现有管理端点共用一个
`API_KEY`，只能记录审核状态，尚不能区分 Agent 与人工审批者。

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

待交付：

- 更完整的首次启动向导与运行时能力探测。
- 独立调用者身份、最小作用域/RBAC 和不可由 Agent 自签的发布审批；在此之前不宣传 human gate。
- 把**首个 Stable 中国平台**作为本阶段发布主目标：优先验证知乎文章，其次验证 B 站视频；
  必须先获得结构化 post identity/readback，并连续 30 天跑专用账号 canary，未达标继续标 Experimental。
- 将写入结果拆成 `accepted`（执行后端接受）、`deployed`（平台部署完成）、`verified`
  （目标 URL/平台后台可回查）；GitHub source branch SHA 目前只能到 `accepted`。
- 其他平台保持保守标记，不为平台数量降低证据门槛。

成功指标：新用户首次 demo 中位时间、安装成功率、首个待审内容建立率。

## Phase 2 — Agent-native contract

目标：让 Agent 调用稳定契约，不需要理解平台 selector 或数据库细节。

- `stage_content`
- `request_approval`
- `plan_publication`
- `schedule`
- `get_job_status`
- `collect_metrics`
- `review_performance`

控制面对 Agent 的交付形式优先级：稳定 Python/HTTP 契约 → CLI → MCP。平台执行层则优先选择
有版本、结构化 post identity 和可回滚路径的官方 API/CLI。两层不要混为一谈，Agent 不能通过
任何一层越过人工审批策略。要兑现这一目标，必须先增加独立的调用者身份、作用域/RBAC 和可验证的
审批主体；在此之前由外部网关或工作流隔离 Agent 与管理 key。详见 [CLI Adapter 选型](cli-adapters.md)。

成功指标：Agent 调用成功率、审批绕过事故数、任务状态可解释率。

## Phase 3 — Evidence and feedback moat（未来能力，不是当前事实）

- Publisher Plugin SDK 和适配器兼容性 manifest。
- 平台 canary、最后验证日期、上游版本与失效报警。
- 跨平台 Metrics 归一化、数据来源和质量标记。
- 内容变体/实验追踪，把指标可解释地回流给 Agent。
- 将 `jobhunt` 等非 Creator Ops 垂直能力迁出 core，改为独立插件或示例。

成功指标：真实发布成功率、重复发布事故数、指标可用率、适配器修复时间。

## 明确不做

- 不自己重写平台签名、反爬或视频编码引擎。
- 不用“支持 N 个平台”代替真实 E2E 证据。
- 不默认让 Agent 自主执行不可逆发布。
- 不把个人内网端点、绝对路径或通知目标带入开源默认配置。
