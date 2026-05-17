# 风控对抗方案（重点：小红书）

> 小红书是国内反爬最严平台。**单点突破不行**，必须**五层立体防御**。
> 本方案不是"破解风控"——是把行为做得**更接近真实用户**，符合平台规则。

## 风险等级

| 平台 | 风控等级 | 主要手段 |
|------|---------|---------|
| 小红书 | ★★★★★ | 设备指纹 + 内容查重 + 行为分析 + 蒲公英审核 |
| 抖音 | ★★★★ | DeviceID + IP + 行为模式 |
| 视频号 | ★★★ | 微信生态封闭，账号关联强 |
| 快手 | ★★★ | 内容指纹 + 频率限制 |
| 知乎 | ★★ | 主要查广告/敏感词 |
| 头条/百家号 | ★★ | 内容审核为主 |
| B站 | ★★ | 上传成本高自然过滤掉机器号 |

## 五层立体防御

### 第一层：账号生态（最重要，治本）

| 措施 | 说明 | 成本 |
|------|------|------|
| **一机一号一IP** | 每账号绑定独立设备指纹 + 独立 IP，**绝不共享** | 高 |
| **养号 7-14 天** | 注册后只浏览/点赞/收藏/关注，**不发布** | 时间 |
| **资料完整** | 头像、简介、性别、地区、个性化标签全部填 | 低 |
| **真人化行为** | 看推荐流 → 进详情 → 停留 30s+ → 点赞/评论 | 时间 |
| **蒲公英备案** | 企业号/MCN 走官方平台，受限但合规 | 资质 |

**结论**：没养号期就发布 = 100% 限流。这是**死规则**，工具再强也救不了。

### 第二层：浏览器层（技术对抗）

我们的 `config.py:browser_engine` 支持四档：

| 档位 | 实现 | 检测率 | 推荐场景 |
|------|------|--------|---------|
| `playwright_chromium` | Playwright 默认 Chromium | 高 | 仅测试 |
| `playwright_chrome_channel` | `channel="chrome"` 用真 Chrome | 中 | 默认/低风控 |
| `patchright` | [Kaliiiiiiiiii-Vinyzu/patchright-python](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python) | 低 | 中等风控 |
| `camoufox` | [daijro/camoufox](https://github.com/daijro/camoufox) Firefox + C++ 指纹欺骗 | 0% | **小红书首选** |

**Camoufox 是 2026 年公认的反检测最强方案**，HeadlessX (1.9k⭐) 等知名项目都基于它。
SAU/XHS Skills 上游默认用 Playwright Chrome channel，对小红书来说**不够**。

接入方式（在 wrapper 里）：
```python
# camoufox 是 drop-in replacement for playwright，把 launch 替换即可
from camoufox.async_api import AsyncCamoufox
async with AsyncCamoufox(headless=False, proxy=...) as browser:
    page = await browser.new_page()
```

### 第三层：内容层（避免被判营销/重复）

| 措施 | 实现 | 我们项目的位置 |
|------|------|---------------|
| **图片去重** | 改 EXIF + 微小裁剪 + 调色 + 加水印 | `content/asset_processor.py` (TODO) |
| **视频去重** | 转码 + 关键帧偏移 + 音频指纹扰动 | 同上 |
| **文案多样化** | 同主题多账号必须 LLM 改写，不能复制粘贴 | `content/generator.py` 加 `vary()` |
| **敏感词过滤** | 自维护词库 + 平台默认词库 | `content/filter.py` (TODO) |
| **simhash 查重** | 同账号 7 天内相似度 < 0.85 | `core/dedup.py` (TODO) |
| **跨账号差异化** | 同主题不同账号 simhash < 0.6 | `cross_account_dedup_threshold` |

### 第四层：行为层（节奏控制）

`config.py` 已暴露的关键参数：

| 参数 | 默认 | 含义 |
|------|------|------|
| `publish_max_per_day` | 2 | 单账号每日发布上限（新号设 1） |
| `publish_min_interval_seconds` | 14400 (4h) | 同账号两次发布最小间隔 |
| `nurture_days` | 7 | 养号期，期内不发布 |
| `rate_limit_per_day` | 5 | 全局兜底（任何账号每日上限） |

**绝对禁忌**（小红书上踩任何一个 = 直接限流）：
- ❌ 凌晨 0-6 点发布（人少 + 算法降权）
- ❌ 整点发布（如 9:00:00 — 太规整）
- ❌ 发布完立刻打开个人主页查看
- ❌ 同账号短时间内反复编辑/删除
- ❌ 文案大量复制其他账号

**正向操作**：
- ✅ 错峰：早 7-9 / 午 12-14 / 晚 19-22
- ✅ 时间打散：实际发布时间 = 计划时间 + random(0, 600)秒
- ✅ 发完去看推荐流 30s+ 再退出（模拟真人）

### 第五层：合规渠道（终极兜底）

| 渠道 | 适用 |
|------|------|
| **蒲公英平台** | 商业合作合规渠道，必须企业号 |
| **聚光投放** | 付费广告，零封号风险 |
| **薯条** | 单条加热，不算违规 |
| **品牌合作人** | 专业号商业能力 |

走合规渠道 + 自然分发结合，是**长期主义**的唯一解。

## 我们项目的风控加固清单

已落地 ✅：
- [x] `config.browser_engine` 可切 4 种引擎
- [x] `config.browser_headless` 默认 False（高风控建议）
- [x] `config.browser_proxy` 全局兜底代理
- [x] `config.publish_min_interval_seconds` 默认 4h
- [x] `config.publish_max_per_day` 默认 2
- [x] `config.nurture_days` 默认 7
- [x] `xhs_skills.py` 的 `--headless` 跟随 settings 切换
- [x] `accounts.manager.is_in_nurture_period()` 养号期判断
- [x] `accounts.manager.check_rate_limit()` 发布前限流校验
- [x] **接入 Camoufox 主链路** — `publishers/xhs_camoufox.py`（`BROWSER_ENGINE=camoufox` 时自动接管，priority=5 顶到 SAU / XhsSkills 之前）
- [x] **per-account 代理** — `AccountIn.proxy` / `Account.profile["proxy"]`；`accounts.manager.get_account_proxy()` 取值
- [x] **per-account 固定指纹** — `get_account_fingerprint()` 按 account_id 派生稳定 OS + 屏幕，同账号每次 launch 同指纹
- [x] **Camoufox 持久化 profile** — `data/browser_profiles/xhs/acc_<id>/`，Firefox profile 全套保留
- [x] **发布时间打散** — `scheduler/jitter.py` + `queue.schedule_publish()`，默认 +random(0,600)s，凌晨 0-7 强制推到白天
- [x] **真人化前置/后置浏览** — `XhsCamoufoxPublisher._human_browse` 发布前后各刷一次推荐流
- [x] **真人化输入节奏** — `_human_type` 逐字 35-110ms + 4% "想一下"停顿；Camoufox `humanize=True` 管鼠标轨迹
- [x] **文案反 AI 检测 humanize** — `content/humanize.py`（句长方差 / 转折词替换 / 标点扰动 / 注入口语词），生成器自动接入，发布层兜底再洗一次
- [x] **风控横幅检测** — 发布前扫描"账号异常 / 操作频繁 / 账号受限"，命中直接返回
- [x] **风控降权自动闭环** — `accounts/health_monitor.py`：24h 节点 metric 回流后比对 7d baseline 中位数，views 跌破 20% 累计 3 次 → DEGRADED + pause 48h；连续 5 次 → BANNED + pause 7d。paused_until 落 `account.profile["paused_until"]`（ISO 字符串），worker.py 在 rate-limit 后追加 is_paused 检查。
- [x] **图片去重 asset_processor** — `content/asset_processor.process_image()`：EXIF（Software/Make/Model/DateTimeOriginal）+ 四边 1-3px 随机裁剪 + 亮度/对比度/饱和度 ±3% + 微旋转 0.1-0.3°；seed 由 `(account_id, src_filename)` 派生，同账号同图幂等，不同账号不同结果；输出到 `data/processed/acc_{id}/`。worker.py 在 XHS + IMAGE_TEXT 路径接入。
- [x] **simhash 查重骨架** — `core/dedup.py`：`compute_simhash` / `hamming_distance` / `is_too_similar`；优先用 `simhash` 包，未装时回退到内置 64-bit md5-based 实现。**仅暴露接口，未接发布主流程**，下个 sprint 接入。

待补 ⏳（优先级排序）：
- [ ] 蒲公英 / 聚光接口适配器
- [ ] `health_monitor.py` 定时扫账号健康度（当前是事件驱动；定时巡检还未做）
- [ ] `core/dedup.is_too_similar()` 接入 `content/generator.py` 与 `xhs_camoufox` 发布前置校验

## 参考资料

- Camoufox: https://github.com/daijro/camoufox
- Patchright: https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python
- HeadlessX (基于 Camoufox): https://github.com/saifyxpro/HeadlessX
- 小红书蒲公英: https://pgy.xiaohongshu.com/
