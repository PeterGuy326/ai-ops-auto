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
   | manual collect or durable 1h/24h/7d task
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

自动回采的 `MetricsCollectionTask` 另存固定 `window_seconds`、不可移动的 `due_at`、
`collection_deadline_at`、下一次重试时间、尝试次数和 expiring owner lease。每个 job/window 唯一，
每个 task 最多绑定一条 `source=scheduled` 的 Metrics；手动/initial 快照不能占用这个绑定。

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

发布成功后，代码以 `finished_at` 为锚点持久化 1h/24h/7d 三个数据库任务，不再注册 APScheduler
一次性 callback。worker 每轮做有界扫描，以条件 claim、64 字符 fencing token、过期 lease、独立
并发上限和有界指数退避执行；任务与唯一快照在一个事务内结束，因此重启或重复扫描不会生成同一
窗口的第二条指标。

这里的唯一性只约束数据库快照，不是平台读取的 exactly-once 保证。若进程在 collector 已返回、
但事务尚未提交时崩溃或被终止，lease 过期后的恢复可能再次读取同一平台数据；fencing token 会阻止
旧 owner 随后提交第二条快照。

固定窗口也有诚实性截止时间：1h、24h、7d 任务分别最多迟到 1h、6h、24h；越过截止时间后任务
明确失败，不把数天后的当前累计值标成早期窗口证据。新的 24h task 当次健康评估按
`window_seconds=86400` 和绑定 metric 精确取数，手动采集或 7d 快照不会替代本次证据；没有
task-bound 快照的 legacy 历史 job 仍保留 latest-metric 兼容回退。

`AUTO_PUBLISH_ENABLED=false` 只阻止新的后台发布，不停止已有成功 job 的只读指标任务。要完全停止
平台访问必须停止 worker。没有 post identity、真实 collector 或有效凭证时任务会失败/跳过，账本
不会生成合成 0，也不把控制面持久性等同于平台数据可用性。

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

- 为每个平台记录指标字段语义、最后验证日期和数据质量等级。
- 增加覆盖率/缺失率，让报表区分“0”和“未知”。
- 建立人工回填与来源标记，不让手填数据冒充自动采集。
- 在证据充分后再做跨平台归一化和 Agent 绩效复盘契约。
