# Metrics Feedback SOP（L5 监测层）

> 没有数据回流的运营是盲投。这一层是整个 5 层闭环的"反馈环"。**数据采集 → 中枢回写 → 日报 → 周报**，全链路走统一 CLI，按 cron 排程自动出报。

## 一、指标定义

| 平台 | 核心指标 | 互动指标 |
|---|---|---|
| 小红书 | 展示量、阅读量 | 点赞、收藏、评论、分享 |
| 头条号 | 推荐量、阅读量 | 点赞、评论、转发 |
| 公众号 | 阅读量 | 点赞、在看、收藏、转发 |

回流粒度：**(account_id, content_id, platform)**——每个 row_id 一行数据，跟账号矩阵对齐。

T+1 拉取，通过 `your_cli content update` 写入中枢对应行（详见 [content-package-format.md](./content-package-format.md)）。

## 二、获取方式（优先级降序）

```
[最优] 平台官方 API
   ↓ 大部分平台无开放 API
[次选] 创作者后台爬虫（复用 sau cookie + patchright 直爬）
   ↓ 反爬严重时
[兜底] IM 提醒人工录入，配置文件/中枢一键提交
```

各平台现状（按你自己环境 verify）：

| 平台 | API | profile 复用 |
|---|---|---|
| 小红书 | 无开放 API | 复用 L4 的 Chrome profile 爬创作者后台 |
| 头条号 | 有"创作中心"数据接口（需登录态） | 复用 L4 profile |
| 公众号 | 有数据接口（需 token） | 复用 L4 profile |

## 三、采集频率

| 数据 | 频率 | 触发方式 |
|---|---|---|
| 发布后 24h 内 | 每 4 小时一次 | 抓"首日爆款"曲线 |
| 发布后 24h-7d | 每天一次 | 抓"长尾"曲线 |
| 发布 7d 之后 | 不再追，status 推进到 `stat-locked` | 数据稳定 |

实现：cron 定时跑 `your_cli metrics pull` → 内部调 `your_cli content update` 批量回写。

## 四、自动出报：日报

每日 18:00 自动跑 `your_cli report daily`，内部调用日志/日报模板：

```bash
your_cli report daily \
  --template "ops-daily-report" \
  --date $(date +%Y-%m-%d) \
  --content-file ./daily-report-$(date +%Y-%m-%d).md
```

日报内容（从中枢聚合）：

```
[运营日报] {date}

今日发布：{N} 条
  - 小红书：{n_xhs}（{accounts}）
  - 头条号：{n_tt}
  - 公众号：{n_gzh}

今日发布主题分布：
  - 产品介绍：{n} / 功能教程：{n} / 集成玩法：{n}
  - 客户故事：{n} / 版本动态：{n} / 社群运营：{n}

24h 内表现 TOP 3：
  1. 《{title}》- {account_id} - {platform} - {views} 展示 / {engagement} 互动
  2. ...

发布失败：{n_fail}（需人工处理）
登录态失效账号：{accounts}
```

## 五、自动出报：周报

每周一上午 9:00 自动跑 `your_cli report weekly`，内部写入 Wiki/知识库或本地归档：

```bash
your_cli report weekly \
  --path /ops/weekly/$(date +%Y-W%V).md \
  --content-file ./weekly-report.md \
  && your_cli notify webhook \
  --url $WEBHOOK_OPS \
  --text "本周宣发周报已生成：{{wiki_link_template}}/$(date +%Y-W%V)"
```

周报模板：

```
[宣发周报] W{N}（{date_start} - {date_end}）

本周发文：{N} 篇 × {M} 账号 = {N*M} 条投放
总曝光：{total_views}（同比 {±X}%）
总互动：{total_engagement}（同比 {±X}%）

主题 ROI 排行：
  1. 集成玩法：CPM {x}，互动率 {y}%（建议加大）
  2. 功能教程：CPM {x}，互动率 {y}%
  3. ...

爆款 TOP 3（按 互动率 排序）：
  1. 《{title}》| {topic} | {platform} | {account_id}
  2. ...
  3. ...

账号矩阵表现：
  - 最高 ROI 账号：{account_id}（建议加投）
  - 最低 ROI 账号：{account_id}（建议人设 review）

product_features 热度：
  - feature_a：{n} 篇 / {views} 曝光
  - feature_b：{n} / {views}
  - integration-llm：{n} / {views}
  - ...

prompt 归因：
  - 高分模式："{successful_prompt_pattern}"
  - 低分模式："{failed_prompt_pattern}"

下周计划：
  - 复用模式：{pattern}
  - 实验方向：{hypothesis}
  - 重点 push 的 product_features：{features}
```

## 六、闭环到 L1（最关键）

**数据驱动 prompt 迭代**是整个 SOP 的反馈环：

```
L5 数据回流（中枢） → 识别高/低 ROI 模式 → 反哺 L3 prompt → 影响 L1 主题选题 → 下次更准
```

具体机制：

- 每月一次 prompt 迭代会，review 周报合集
- 高 ROI product_features → 加入下个月 L1 选题排期
- 高 ROI 风格 → 加入 prompt 的"推荐范式"section
- 低 ROI 风格 → 加入 prompt 的"避免范式"section
- 平台算法变化（如小红书禁某些词）→ 触发 prompt review（不超过 2 周一次），结论归档到 `/ops/incidents/`

迭代痕迹沉淀在：

```
prompts/platform_style/xiaohongshu-style.md   # 当前版本
prompts/archive/xiaohongshu-v0.md             # 历史版本
prompts/xiaohongshu-failures.md               # 失败案例库
```

镜像同步到 Wiki/知识库的 `/ops/prompts/`，非工程同学也能查。

## 七、风险与对策

| 风险 | 对策 |
|---|---|
| 反爬升级让数据停摆 | 兜底 → IM 提醒人工录入，不阻塞业务 |
| 数据时延（T+0 困难） | 接受 T+1，T+0 不在 MVP 范围 |
| 指标主义陷阱 | 周报必须包含"互动质量观察"段落，不只看数字 |
| Cookie 过期 | 自动检测 + IM 通知触发重新登录 |
| 日报/周报调用失败 | 报表本地兜底落到 `./reports/` 目录，事后 retry |

## 八、跟其他层的接口

| 来源 | 数据流 | 接口 |
|---|---|---|
| L4 发布层 | 提供 `published_url, published_at` | `your_cli content query --filter status=published` |
| L5 自身 | 写 `views, likes, ..., last_synced_at` | `your_cli content update` × N |
| L5 → 日报 | 当日汇总 | `your_cli report daily --template ops-daily-report` |
| L5 → 周报 | 周回顾 | `your_cli report weekly --path /ops/weekly/...` |
| L5 → 群通知 | 周报发布提醒 | `your_cli notify webhook --url $WEBHOOK_OPS` |
| L1 prompt 迭代 | 读 wiki 月度合集 | 人工 review + 改 prompts/*.md + 镜像回 wiki |

## 九、待办（参考）

- [ ] 确认各平台数据源（API / 爬虫 / 手动），更新本文档表格
- [ ] 实现 `your_cli metrics pull` 定时拉数脚本
- [ ] 实现 `your_cli report daily` 自动出日报
- [ ] 实现 `your_cli report weekly` 自动出周报
- [ ] 日报模板预创建
- [ ] Wiki/知识库目录树预创建（`/ops/weekly/`、`/ops/prompts/`、`/ops/incidents/`）
- [ ] prompt 迭代会的 SOP（每月做、做什么、产出什么），结论归档到 Wiki
