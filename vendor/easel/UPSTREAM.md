# Upstream: Easel

- 上游仓库：https://github.com/zju-real/Easel
- 引入 commit：011e10d（2026-11-17，v0.3.0）
- 引入日期：2026-11-18
- 许可证：Apache-2.0（见本目录 LICENSE，保留上游署名）
- 引入方式：curated vendoring（人工挑选目录复制，非 git subtree）
- 排除清单：.git / .venv / node_modules / dist / __pycache__ / outputs（运行时）/
  assets/readme（宣传视频 235M）/ web/static/showcase（宣传视频 202M）
- 同步策略：不自动追上游；需要时人工 diff patch，更新本文件 commit 记录
- 本地增量：无（Phase A 原样引入）；治理桥接在 src/ai_ops 侧以插件实现，见
  ../../docs/easel-refactor-plan.md
