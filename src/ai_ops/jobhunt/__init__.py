"""jobhunt 专题 —— 简历分析 → 全平台自动投递。

形状上区别于现有「内容分发」流水线：
  - 内容分发：1 份内容 → N 账号 → 广播（fan-out）
  - 求职投递：N 个岗位（爬来的）→ 匹配打分过滤 → M 份个性化投递 → 追踪 HR 回复（funnel + match）

复用现有底座：accounts / runtime(playwright+stealth) / scheduler(限流 jitter) / dedup / api(UI+alembic)。
新建业务层：ResumeProfile / JobPosting / JobMatch / Application 四张表 + 各平台 Applier 适配器。

分阶段：
  P0  数据模型 + 简历解析（纯离线，零平台风险）   ← 当前
  P1  Boss 岗位采集 + LLM 匹配打分 → 候选池(DRAFT)，人工勾选
  P2  Boss 投递执行（发打招呼语），复用 scheduler 风控
  P3  HR 回复轮询 → 钉钉通知
  P4  扩展 智联 / 猎聘 / 51job（表单式）
"""
