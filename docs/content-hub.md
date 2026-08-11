# 素材管理中台 · 使用说明

> 一句话：**AI 生成 / 历史回填 → 统一素材库 → 先审后发 → 按个人账号分发并留痕**。
> 开源默认不配置任何内网/云服务或通知目标；先审核再分发。
> 当前 `approve` 是共享管理 `API_KEY` 下的状态迁移，不会验证调用者一定是人；强制人工审批需由
> 外部权限/工作流保证。

## 1. 底层逻辑（一张图）

```
AI 生成（短剧 / 播客 / 博客 / 图文）┐
                                   ├─→ 素材库(Article)  ──状态机──→ 分发(PublishJob, 每账号一条)
历史已发（导出文件回填 / 在线采集）  ┘   DRAFT  待审             ──→ worker(风控/限流/metrics) ──→ 平台
                                        READY  审过
                                        SCHEDULED 已分发
                                        PUBLISHING / PUBLISHED
```

- **素材** = `Article`（含文章/视频/博客/播客，content_type=IMAGE_TEXT/VIDEO/LONG_ARTICLE/AUDIO）+ `Asset`（视频/图片/音频文件）。
- **分发记录** = `PublishJob`（挂 `account_id` + platform + status + platform_url）——天然按**个人账号**留痕。
- **状态门槛**：所有内容先落 DRAFT，调用 `approve` 进入 READY 后才能 `distribute`；当前 API
  只校验管理 key，不校验审批者身份。

## 2. 核心模块

| 模块 | 职责 |
|------|------|
| `content/distributor.py` | 入库 / 审核 / 分发 / 留痕 / 历史回填 |
| `content/collector.py` | 平台导出文件（CSV/JSON）→ 历史回填 |
| `pipeline/{script_to_drama,topic_to_podcast,topic_to_blog}.py` | 三场景生成 |
| `video/happyhorse.py` | 可选 HappyHorse 兼容协议文生视频适配器 |
| `scheduler/worker.py` | 消费 PublishJob 真发布（含风控闭环） |
| `api/main.py` `/ui/accounts/{id}` | 账号详情页（历史+新发记录可视化） |

## 3. 常用流程

### 3.1 生成即入库（待审）
```python
from ai_ops.content import distributor
from ai_ops.pipeline import ScriptToDramaPipeline
from ai_ops.pipeline.script_to_drama import DramaRequest
from ai_ops.core.schemas import VideoBrief
from ai_ops.core.enums import Platform

plan = await ScriptToDramaPipeline().plan(DramaRequest(
    brief=VideoBrief(theme="逆袭短剧", script="...", duration_seconds=10, resolution="1080x1920"),
    platforms=[Platform.DOUYIN, Platform.XIAOHONGSHU], title="逆袭短剧·第一集",
))
arts = distributor.stage_clip_plan(session, topic_id, plan, title="逆袭短剧·第一集")  # → DRAFT 待审
```
播客 `stage_podcast_result`、博客 `stage_blog_content` 同理。

### 3.2 审核 → 按账号分发
```python
distributor.approve(session, article_id)                       # DRAFT → READY
jobs = distributor.distribute(session, article_id,             # 仅 READY 可分发
                              account_ids=[1, 2])              # 省略则按 target_platforms 自动选号
# 每个账号一条 PublishJob(PENDING)；真发布由 scheduler.worker 拉起
```

### 3.3 历史发布回填（之前/手动发的也纳入管理）
```python
from ai_ops.content import collector
collector.import_from_csv(session, account_id, "抖音导出.csv")   # 列名容错：标题/作品链接/作品id/发布时间/类型
# 或 collector.import_from_json(session, account_id, "posts.json")
```
> 价值：① 按账号记录补全历史 ② **喂查重**（避免重复生成你发过的）③ 数据统计完整。幂等，可反复跑。

### 3.4 按账号查记录 / 看后台
```python
recs = distributor.list_account_jobs(session, account_id)      # 历史 + 新发
```
浏览器打开 `/ui/accounts/{account_id}` 看该号全貌。

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/articles/{id}/approve` | 审核通过 DRAFT→READY |
| POST | `/articles/{id}/distribute` | 按账号分发（body=account_ids，可空=自动选号） |
| GET  | `/accounts/{id}/jobs` | 按账号查分发记录 |
| POST | `/accounts/{id}/import-published` | 批量回填历史发布 |
| GET  | `/ui/accounts/{id}` | 账号详情页 |

## 5. 视频引擎（短剧）配置

HappyHorse 是可选的兼容协议适配器。开源配置不附带端点或凭证；使用者必须填写自己有权访问的服务：

```dotenv
WUKONG_API_KEY=
WUKONG_VIDEO_JOBS_URL=
WUKONG_VIDEO_MODEL=happyhorse-1.0-t2v
```

LLM 默认使用公开 OpenAI 端点；也可配置你自己有权使用的 OpenAI 兼容网关。不要把 key 或私有端点提交到仓库。

## 6. 现状与边界

- 已有生成 / 历史回填 / 审核 / 按账号分发 / 留痕 / 后台可视化的代码与自动化测试。
- ⚠️ **真上传平台**（抖音等）需在能装 `social-auto-upload` + 账号 cookie 的机器上跑 worker；
  它们不是当前可用承诺；以 [平台能力矩阵](platform-capabilities.md) 为准。
- ⚠️ 播客 ListenHub 需自备 Free key（`LISTENHUB_API_KEY`）。
