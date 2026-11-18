# ai-ops-auto × Easel 仓库合并方案

日期：2026-11-18
依据：Easel 底座评估报告（data/outputs/easel-eval-20261118/）、Phase 1 evidence card-001、两仓库实机勘察。
状态：方案待确认；执行在 `refactor/easel-base` 分支，main 不动。

## 0. 硬约束

- 不新建仓库：保留 `PeterGuy326/ai-ops-auto` 的 star、历史与影响力。
- 许可证：本仓 Apache-2.0，Easel Apache-2.0，兼容； vendored 代码保留上游 LICENSE 与署名（UPSTREAM.md）。不并入任何 GPL/AGPL 代码（Postiz/Wechatsync 只可外挂，千帆云递仅参考 UI）。
- 治理层是差异化核心：`agent_contract`（独立审批 principal/幂等账本/不可变快照）、`scheduler`（持久 worker/lease/fencing）、`plugin_sdk` 保留并加强，不重写。
- 铁律：只存草稿、绝不自动发表；不可逆操作留给人。
- main 当前有 4 个未提交修改（.env.example、README.md、docs/publishing-sop.md、frontend/src/App.tsx，用户在途工作）：开分支时原样携带，不丢、不擅自提交。

## 1. 两侧现状（实勘）

ai-ops-auto（main@82fd2c1）：
- `src/ai_ops/`：agent_contract / scheduler / publishers（plugin_sdk，entry-point group `ai_ops.publishers.v1`）/ core / runtime / accounts / content / api / cli。
- SQLAlchemy + Alembic 持久层；旧 React `frontend/`；CLI `ai-ops`；Python >= 3.11。
- 平台矩阵 13 个全 Experimental/Stub，无真实 Stable——发布执行层是短板。

Easel（zju-real/Easel @ 011e10d，v0.3.0，Apache-2.0，活跃）：
- `easel/` CLI；`web/app.py` 单文件 FastAPI（约 6200 行）；`web/frontend/` React；`skills/` 118 个 builtin。
- 素材库=outputs/ 全量扫描 + 项目 `.easel.json` 展示头；账号页 10 平台扫码；发布中心一键分发+预检。
- 公众号走后台扫码会话：免 AppID/白名单、未认证号可用、发布=建草稿、stats 真实读回。
- 存储 JSON + threading.Lock；发布跑 daemon 线程（无重试/fencing）——治理是短板。
- 已有治理桥接口：`EASEL_PUBLISH_MODE=manual` → `easel/manual_publish.py build_package` 导出包，不真发。

互补结论：Easel 出身体（素材/账号/分发/公众号链路），ai-ops 出灵魂（审批/持久/幂等/审计）。

登录态：`~/.easel-browser-profiles/`（WeixinMpProfile、ZhihuProfile 等）在用户 home 级，**合并副本自动继承已扫码登录态**，无需重扫。

体积：Easel 全量 472M，其中宣传视频 assets/readme 235M 与 web/static/showcase 202M 重复、node_modules 136M——curated vendoring 排除后实际引入约 23M（skills 含 5.8M 字体，可选再裁）。

## 2. 目标结构

```
ai-ops-auto/
  src/ai_ops/            # 治理核心，保留
  vendor/easel/          # curated vendored Easel
    easel/ web/ skills/ scripts/ config/ agents/ profiles/ docs/ tests/
    pyproject.toml LICENSE README.md UPSTREAM.md
    outputs/             # 运行时数据，.gitignore
  frontend/              # 旧 UI 冻结；审批页保留至 Phase B 迁移完成
  docs/easel-refactor-plan.md
  data/                  # 运行时资产/证据，不入 git
```

- 双包同 venv：`pip install -e . -e vendor/easel`；CLI `ai-ops` 与 `easel` 并存，入口统一放 Phase C。
- `vendor/easel/outputs/` 加入 .gitignore；`OUTPUTS_DIR` 硬编码项目根（web/app.py:176），不改代码，用目录约定。
- UPSTREAM.md 记录：上游 URL、commit 011e10d、引入日期、Apache-2.0、排除清单、同步方式（人工 diff patch，不追自动 subtree）。

## 3. Phase A — 引入与跑通（refactor/easel-base 分支）

1. `git checkout -b refactor/easel-base`（携带 WIP，不提交）。
2. curated 复制：排除 `.git/.venv/node_modules/dist/outputs/assets/readme/web/static/showcase`。
3. 写 UPSTREAM.md；根 README 增「底座」一节指向 vendor/easel 与本方案。
4. 同一 venv 双 `-e` 安装；`easel web --port 7860` 与 `ai-ops doctor` 均通过。
5. 迁移运行时资产：评估副本的 6 个内容库项目（5 文章+素材库）复制到 `vendor/easel/outputs/`；登录态 home 级自动继承。
6. `EASEL_PUBLISH_MODE=manual` 写入 vendor/easel 环境配置——Easel 侧默认不真发，导出包走治理。
7. 验收：内容库 6 项目可见、账号 2/10（公众号+知乎）、发布中心 manual 模式、FFmpeg/模型 Key 待办记录在案。

## 4. Phase B — 治理桥接（差异化落地）

1. Publisher 插件：把 `weixin_mp_stats.py publish`（公众号草稿）与知乎 publisher 包装为 `ai_ops.publishers.v1` entry-point 插件，namespaced kind `easel.wechat_oa` / `easel.zhihu`；版本门禁=上游 commit；manifest/doctor 走现有 plugin_sdk。
2. readback 模块：Phase 1 读回脚本固化进插件——草稿箱服务端 `window.wx.cgiData`（标题/摘要）+ 编辑器 UI 导航 `#ueditor_0 .ProseMirror`（正文逐段）；预览临时链接不可用（环境验证页）、GET 被 CSRF 拦、直拼 URL 报重新登录等坑写入模块 docstring；比对剔除 META 注释、代码围栏内不剥 markdown；产出 evidence card，fail-closed。
3. 执行链路：Easel manual 导出包 → `plan_publication` → 独立 human 审批 → `schedule` → 持久 worker（lease/fencing/有界重试）调插件 → readback 核验 → 1h/24h/7d MetricsCollectionTask（公众号 stats collector 接通）。
4. Easel `_schedule.json` 仅作展示日历；真实排程与状态在 ai-ops DB；daemon 线程直发路径在 manual 模式下不启用。
5. 验收：一篇真实文章走完整闭环（只存草稿）；重启不丢任务、不重复发布；审批绕过事故 0；evidence card 可独立复核。

## 5. Phase C — 产品化（打包出售前）

- 多账号矩阵：platform 键控 → account id 数据模型扩展。
- JSON → SQLite/Postgres：复用 alembic；Easel 存储层逐步替换。
- 素材库独立入库/标签/复用视图。
- 审批触达：钉钉卡片提醒「有待审批」，决定仍走独立 human 凭证。
- 入口统一（单 CLI/单 Web 壳）、CI 双包矩阵、FFmpeg/模型 Key 配置文档。
- 商业化说明：Apache-2.0 署名/NOTICE 保留；治理层作为自有增量卖点。

## 6. 明确不做

- 不整体重写 web/app.py（6200 行单文件，先桥接后渐进）。
- 不删旧 core/旧 UI（冻结保留，审批页迁移后再议）。
- 宣传视频/node_modules/运行时 outputs 不入 git。
- 不并 GPL/AGPL；不自动发表；不用平台数量当指标。

## 7. 回滚

- 全部改动在 refactor 分支，main 与 WIP 不受影响；vendor 目录可整体移除回退。
- 评估副本 `~/data/easel-eval` 保留为对照基线，直至 Phase B 验收通过。