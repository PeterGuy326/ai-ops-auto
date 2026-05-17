# Content Package Format

> 生成层（L1）的输出契约 = 中枢层（L2）的输入契约。所有下游按此 schema 解析，schema 不对直接 reject。
>
> **范围**：本 schema 承接面向单一产品/品类的内容宣发。`topic` 必须落在主题白名单内，不在的直接 reject。

## 一、文件命名

一个内容包 = 一个 markdown 文件，命名约定：

```
{YYYY-MM-DD}-{topic-slug}.md
```

- 日期：内容生成日期（不是发布日期）
- topic-slug：短横线分隔小写英文，≤ 5 个词
- 示例：`2026-05-14-credit-approval-ai.md`、`2026-05-15-warehouse-rfid.md`

为什么不用纯数字递增 ID：

- 时间前缀让文件按时间排序自然有序
- topic-slug 让人看到 ID 就知道是什么
- 不依赖中心化计数器，多人写也不会冲突

## 二、Frontmatter Schema

```yaml
---
# 必填字段
id: 2026-05-14-cli-intro                   # 唯一 ID，同文件名 stem
title: 把工作台装进命令行：项目是什么、为什么做  # 主标题，≤ 50 字
topic: 产品介绍                             # 主题分类，必须在白名单内
product_features:                          # 本文涉及哪些产品能力（≥ 1 个）
  - cli-overview
  - feature_a
target_word_count: 1500
target_channels:
  - xiaohongshu
  - toutiao
  - gongzhonghao
target_accounts:                           # 目标账号矩阵（账号 ID）
  xiaohongshu: [acc_xhs_official, acc_xhs_personal]
  toutiao:     [acc_tt_official]
  gongzhonghao:[acc_gzh_main]
created_at: 2026-05-14T17:52:00+08:00

# 可选字段
source: claude                             # 生成来源：llm_a / llm_b / manual
hook: 5 行命令把整个工作台搬进 LLM Agent    # 必须保留的核心钩子
keywords:                                  # 改写时必须保留的关键词
  - {{product_name}}
  - 命令行
  - 开源
banned_words:                              # 平台禁词
  - 最佳
  - 国家级
  - 第一
cta:                                       # 召唤行动
  type: github_star                        # github_star / community_join / docs / demo
  url: {{project_url}}
references:                                # 引用素材
  - type: chat_thread
    url: https://example.com/chat/xxxxx
  - type: github_pr
    url: {{project_url}}/pull/123
assets_dir: ./assets/2026-05-14-cli/       # 配图目录（相对路径）
---

# 正文 markdown
```

## 三、主题白名单（topic）

宣发只产以下 6 类内容，不在白名单的直接 reject：

| topic | 定义 | 例子 |
|---|---|---|
| `产品介绍` | 是什么、为什么做、跟谁比 | "把工作台装进命令行" |
| `功能教程` | 某个命令/能力怎么用 | "一次写 100 行的批量接口" |
| `集成玩法` | 与 LLM/Cursor/Agent/MCP 集成 | "在 LLM 编辑器里跑 IM 操作" |
| `客户故事` | 真实团队怎么用提效 | "某 SaaS 团队周报 SOP 自动化" |
| `版本动态` | release notes / 新功能 | "v0.5 发布：全文检索 + 日志提交" |
| `社群运营` | 社群活动 / Q&A / 复盘 | "社群 100→1000 的 30 天" |

> 行业话题（金融/制造/医疗等）**不在范围内**——产品是工具，不是行业方案。如果出现行业类需求，归类到 `客户故事` 并强制以"客户怎么用产品"为视角，不是"行业 AI 怎么做"。

## 四、product_features 白名单（按产品能力）

按你自己的产品能力定义。每篇至少标 1 个 `product_features`——schema 强制保证内容跟产品强相关，杜绝"标题挂产品名、正文不提"的水分。

示例（请按实际产品替换）：

```
cli-overview        命令行总体能力
feature_a           能力 A
feature_b           能力 B
integration-llm     与 LLM 集成
integration-cursor  与 Cursor 集成
integration-agent   与 Agent SDK 集成
integration-mcp     与 MCP 集成
```

## 五、Body 正文约定

- 第一个 `#` H1 标题跟 frontmatter `title` 字段一致
- 用 `##` 起小节，避免直接跳到 `###`
- 数据放表格或代码块，不要混在叙述里
- 图片占位用 `[图：xxx]`，实际图片走 `assets_dir` 指向的目录

## 六、Schema 校验规则

中枢层（L2）写入前必须通过校验：

| 规则 | 严重程度 | 失败行为 |
|---|---|---|
| `id` 唯一性（跟中枢表已有行不冲突） | 致命 | 拒绝写入 |
| `id` 格式匹配 `YYYY-MM-DD-[a-z0-9-]+` | 致命 | 拒绝写入 |
| `title` 非空且 ≤ 50 字 | 致命 | 拒绝写入 |
| `topic` 在主题白名单 | 致命 | 拒绝写入（无关内容不进流水线） |
| `product_features` 至少 1 个且都在白名单 | 致命 | 拒绝写入（保证强相关） |
| body 中产品关键词出现 ≥ 1 次 | 致命 | 拒绝写入（防"挂名卖私货"） |
| `target_channels` 至少 1 个 | 致命 | 拒绝写入 |
| `target_accounts` 至少 1 个 | 致命 | 拒绝写入 |
| body 非空且 ≥ 200 字 | 致命 | 拒绝写入 |
| `created_at` 合法 ISO 8601 | 致命 | 拒绝写入 |

校验失败 → 报错 + 不写入中枢 + 通过 IM webhook 提醒具体哪个字段不合规。

## 七、跟改写层（L3）的接口

L3 改写 prompt 在调用时会读取以下字段作为上下文：

| 字段 | 用途 |
|---|---|
| `title` | 改写后标题的起点 |
| `topic` | prompt 上下文（"你正在为产品写一篇 {topic} 类内容"） |
| `product_features` | 改写必须围绕这些能力展开，不能跑题 |
| `hook` | 必须保留的核心钩子 |
| `keywords` | 改写不能丢失这些词 |
| `banned_words` | 改写必须规避这些词 |
| `cta` | 结尾必须带这个召唤行动（GitHub star / 入群 / 看文档） |
| body | 主稿内容 |

## 八、未来扩展（暂不做）

- 多语言：`lang: zh-CN`
- 视频/图文混排：`media_type: text|video|image`
- 私有 tag：`tags: [...]`
