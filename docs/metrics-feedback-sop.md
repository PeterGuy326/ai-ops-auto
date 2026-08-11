# Metrics 与报表 SOP（Alpha）

本页描述当前代码能执行的指标/报表契约，以及尚未完成的边界。它不是跨平台数据飞轮的
完成声明；逐平台能力以[平台能力矩阵](platform-capabilities.md)为准。

## 当前数据链

```text
Publisher result
   | optional initial_metadata
   v
Metrics row (source=initial)

successful PublishJob with platform_post_id
   | manual collect or in-memory 1h/24h/7d callback
   v
Publisher.collect_metrics
   v
Metrics row (source=manual/scheduled) -> topic heat / health evaluation
   v
daily / weekly local Markdown report
```

只有同时满足以下条件时，某个 job 才有真实指标回流：

1. Publisher 能确认发布成功，并返回可验证的 `platform_post_id`（可选 `platform_url`）。
2. 该 Publisher 实现了真实 `collect_metrics`，而不是基类的空结果。
3. 登录态/平台 API 仍可用，并且采集结果通过平台特定校验。

缺任一条件时，控制面可以保存 job 和生成报表，但数据不代表真实平台表现。

## 数据模型

`Metrics` 通过 `job_id` 关联 `PublishJob`，保存：

- `views`、`likes`、`comments`、`shares`
- `source`：`initial`、`manual` 或 `scheduled`
- `raw`：适配器返回的已允许原始字段
- `collected_at`

`Account`、`Article` 和平台侧 post identity 通过 PublishJob 间接关联。不同平台对“阅读/播放/展示”
的定义并不一致；当前只做基础字段归一化，不提供可直接比较的跨平台 ROI/CPM 结论。

## 手动采集

对已有 `platform_post_id` 的成功 job，可用鉴权 API 显式采集：

```bash
curl -fsS -X POST \
  -H "X-API-Key: <redacted>" \
  http://127.0.0.1:8000/jobs/<job_id>/collect
```

返回 `skipped=true` 时，按 `reason` 检查 post identity、凭证和 Publisher。HTTP 成功不等于指标
一定来自真实平台；还需结合能力矩阵和 adapter evidence。

## 自动采集边界

发布成功后，代码会尝试注册 1h/24h/7d 三个 APScheduler callback。当前这些 callback 是
**内存任务**，不像 PublishJob 一样持久化：

- worker 重启可能丢失尚未触发的回采计划。
- API 进程不持有 scheduler；显式 API 发布不应被理解为自动回采有保证。
- 没有 post identity 或真实 collector 的平台会跳过或写出无业务价值的数据。

因此关键运营数据应通过人工核验/回填或外部持久采集器补齐，直到 Metrics 任务也进入数据库账本。

## 日报和周报

本地报表命令已经实现：

```bash
ai-ops report daily --date 2026-08-10 --out-dir ./reports --no-notify
ai-ops report weekly --week 2026-W33 --out-dir ./reports --no-notify
```

不传日期时使用 UTC 日期。`ai-ops worker` 会按 `SCHEDULER_TIMEZONE` 注册每日 18:00 日报和
每周一 09:00 周报，并尝试通过已配置的通知 adapter 发送就绪消息。

报表只聚合数据库已有记录。空指标、缺失指标或来源不同的数据不会因为生成 Markdown 就变得完整；
阅读报表时必须同时看数据覆盖率和 `source`。

## 运营核对清单

1. 对照平台能力矩阵确认该 Publisher 是否有真实采集证据。
2. 核对 job 的 post id/url，避免把草稿页或发布页 URL 当公开内容。
3. 记录 collector/upstream commit、采集时间、账号类型和平台字段定义。
4. 对异常的 0 值先判断“真实为 0”还是“采集缺失”，不要直接优化内容策略。
5. 报表通知失败时检查本地 `reports/`；通知不是报表成功的唯一证据。
6. 不在日志、报表或 Issue 中保存 cookie、token、个人账号信息或平台原始敏感响应。

## 待办

- 将 1h/24h/7d 指标节点持久化，支持重启恢复和去重。
- 为每个平台记录指标字段语义、最后验证日期和数据质量等级。
- 增加覆盖率/缺失率，让报表区分“0”和“未知”。
- 建立人工回填与来源标记，不让手填数据冒充自动采集。
- 在证据充分后再做跨平台归一化和 Agent 绩效复盘契约。
