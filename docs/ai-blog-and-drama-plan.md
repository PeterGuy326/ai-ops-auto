# AI 播客 & AI 短剧 —— 云集成方案（本地验证版）

> 状态：调研 + 云适配器实现 + mock 集成验证完成，**未推云端**。
> 口径修正：用户「AI 博客 = ListenHub 这种」实指 **AI 播客**；两个场景一律
> **云 API 集成，本地零算力**（用户本地无 GPU）。
> 结论先行：**两条云链路集成层已 100% 本地验证（362 passed），只待配 API key 真跑。**

---

## ⭐ 最终决策与落地（2026-06-19）

| 场景 | 引擎（云，零算力） | 适配器 | 状态 |
|------|------------------|--------|------|
| **AI 播客** | ListenHub（Marswave） | `podcast/listenhub.py` `ListenHubProvider` | ✅ mock 验证 |
| **AI 短剧** | 可灵 Kling（主力） | `video/kling.py` `KlingEngine` | ✅ mock 验证 |
| AI 文字博客 | Hexo（自有阵地，保留） | `pipeline/topic_to_blog.py` | ✅ dry_run 验证 |

### 真实云契约（2026-06 校对，已落进适配器）

**ListenHub 播客**
- base `https://api.marswave.ai/openapi`，`Authorization: Bearer {LISTENHUB_API_KEY}`
- 建：`POST /v1/podcast/episodes` body `{query, speakers:[{speakerId}], language, mode(quick|deep|debate), sources?}`
- 查：`GET /v1/podcast/episodes/{episodeId}` → `{processStatus(success|failed|processing), audioUrl, scripts, credits}`
- 轮询：首轮等 60s，之后 10s/次

**可灵 Kling 文生视频**
- base `https://api.klingai.com`（可换 api-beijing / api-singapore）
- 鉴权：JWT(HS256) `{iss=AK, exp=+1800, nbf=-5}` 用 SK 签 → `Bearer`（token 30min，每次现签）
- 建：`POST /v1/videos/text2video` body `{model_name, prompt, negative_prompt, duration, aspect_ratio, mode}`
- 查：`GET /v1/videos/text2video/{task_id}` → `data.task_status==succeed` 取 `data.task_result.videos[0].url`
- ⚠️ 生成物 30 天后清理 → `kling_download=True` 自动转存本地

### 配置项（.env，字段名大写）
```
LISTENHUB_API_KEY=...
KLING_ACCESS_KEY=...
KLING_SECRET_KEY=...
# 可选：KLING_API_BASE / KLING_MODEL / KLING_MODE / LISTENHUB_API_BASE
```

### 下一步（配 key 后真跑，仍不推云端）
1. ListenHub：拿 Pro/Max key（Max 才有完整 Audio API），先 `quick` 模式小额验证。
2. Kling：拿 AK/SK，先 5s std 档小额验证 → 跑通再上 pro/10s。
3. 真跑用真 httpx（去掉 mock），其余编排层零改动。

---

## （以下为早期调研记录，方向修正前）

> 状态：本地调研 + PoC 验证完成，**未推云端**。
> 结论先行：**AI 博客 今天可端到端真跑；AI 短剧 编排层就绪，卡点在外部视频引擎选型 + 安装。**

---

## 0. 边界澄清

- **文字类**（知乎/头条/公众号/百家号/搜狐号）= 已实现，本方案不涉及。
- 本方案聚焦两个**新场景**：
  1. **AI 博客** —— AI 生成长文 → 自有 Hexo 博客（GitHub Pages）。
  2. **AI 短剧** —— 脚本 → 视频引擎生成 → 切片 → 多平台（抖音等）。

---

## 1. 现状盘点（已验证）

| 能力 | 模块 | 状态 |
|------|------|------|
| LLM 内容生成 | `content/generator.py`（openai/anthropic/deepseek/dashscope 可切） | ✅ |
| 博客发布 | `publishers/github_pages.py`（Hexo，dry_run 已验证） | ✅ |
| 抖音发布 | `publishers/social_auto_upload.py`（CLI `douyin upload_video` + HTTP type=3） | ✅ 代码就位 |
| 视频生成 | `video/money_printer.py`（MoneyPrinterTurbo） | ✅ 代码就位，⚠️ 非真剧情 |
| 视频切片 | `video/clipper/funclip.py`（FunClip ASR 切片） | ✅ 代码就位 |
| 切片→发布计划 | `pipeline/clip_to_publish.py` | ✅ 已验证 |

**测试基线**：`346 passed`。

**真实缺口（两个场景共用）**：生产代码里没有"内容 → 扇出成 PublishJob"的一键编排入口（只有重发会建 Job）。本方案补的就是这层"场景编排"。

---

## 2. AI 博客方案

### 链路
```
主题/关键词/人设
  → ContentGenerator (LLM)            生成 Markdown 长文
  → PublishContent(LONG_ARTICLE)
  → GitHubPagesPublisher              frontmatter + 正文 → source/_posts/<slug>.md
  → hexo generate → git push          (dry_run 可关)
  → 返回 article_url
```

### 缺口与补法
- 新增 `pipeline/topic_to_blog.py`：`TopicToBlogPipeline`，把上面四步串成一次 `run()`。
- generator / publisher 均可注入，便于本地用 fake LLM + temp git 仓库验证。

### 落地成本
- **零外部账号、零反风控、零 GB 级依赖**——git 即可。
- Phase 1（今天）：配真实博客仓库路径 + LLM key → 关 dry_run → 真发一篇。

---

## 3. AI 短剧方案

### ⚠️ 关键认知：MPT ≠ 真短剧
`MoneyPrinterTurbo` = 关键词 → **素材库视频 + AI 文案 + TTS 口播 + 字幕**。
适合"资讯/口播/科普"短视频，**不是有角色、有分镜、有剧情的短剧**。

真正的 AI 短剧需要：
```
剧本(LLM) → 分镜拆解 → 每镜画面生成(文生图/图生视频: SD/ComfyUI/即梦/可灵)
         → 角色配音(TTS) → 合成拼接 → 字幕 → 成片
```

### 引擎选型（待你拍板，成本/效果权衡）

| 档位 | 引擎 | 效果 | 成本 | 本地可跑 |
|------|------|------|------|---------|
| 轻短剧（口播/图文剧情） | MoneyPrinterTurbo | 中 | 低（已集成） | ✅ |
| 中（分镜静帧+配音） | SD/ComfyUI 文生图 + TTS + ffmpeg 合成 | 中高 | 中（需 GPU/模型） | ⚠️ 需装 |
| 重（真视频生成） | 即梦/可灵/Runway 等云 API | 高 | 高（按量付费） | API 即可 |

### 缺口与补法
- 新增 `pipeline/script_to_drama.py`：`ScriptToDramaPipeline`，**引擎可插拔**
  （吃 `VideoEngineBase`）：脚本 → `engine.render()` → 可选 FunClip 切片
  → 多平台 `PublishPlanItem`。
- 今天用 MPT 验证"轻短剧"全链路；引擎选定后只换注入对象，编排层不动。

### 落地路径
- Phase 2A：`install_external.sh` + `brew install ffmpeg` + FunClip 独立 venv。
- Phase 2B：先单测 MPT 真生视频 → FunClip 真切片 → SAU 抖音真发。
- Phase 2C：若要"真剧情"，接文生图/图生视频引擎（新写一个 `VideoEngineBase` 实现）。

---

## 4. 共用收尾：内容 → 发布任务扇出

两个场景最终都要落到现有 worker 闭环（rate-limit / 风控间隔 / metrics）。
建议补一个 `create_publish_jobs(article, account_ids)` service，把"内容→N 个
PublishJob"标准化，避免绕过风控直发。**本期先做到"发布计划/内容就绪"，
真发布走既有 worker。**

---

## 5. 本地验证证据

- `tests/test_topic_to_blog.py` —— AI 博客全链路（fake LLM + temp git，dry_run）。
- `tests/test_script_to_drama.py` —— AI 短剧编排（fake 引擎 + fake clipper → 抖音计划）。
- 运行：`.venv/bin/python -m pytest tests/test_topic_to_blog.py tests/test_script_to_drama.py -v`
