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
- [x] `config.browser_proxy` 单账号代理
- [x] `config.publish_min_interval_seconds` 默认 4h
- [x] `config.publish_max_per_day` 默认 2
- [x] `config.nurture_days` 默认 7
- [x] `xhs_skills.py` 的 `--headless` 跟随 settings 切换

待补 ⏳（优先级排序）：
- [ ] 接入 Camoufox（`pip install camoufox`，wrapper 里加分支）
- [ ] `accounts.manager.is_in_nurture_period()` 养号期判断
- [ ] `accounts.manager.check_rate_limit()` 发布前限流校验
- [ ] `content/asset_processor.py` 图片/视频去重处理器
- [ ] `core/dedup.py` simhash 查重
- [ ] `scheduler` 的发布时间打散（默认 +random 600s）
- [ ] 蒲公英 / 聚光接口适配器

## 参考资料

- Camoufox: https://github.com/daijro/camoufox
- Patchright: https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python
- HeadlessX (基于 Camoufox): https://github.com/saifyxpro/HeadlessX
- 小红书蒲公英: https://pgy.xiaohongshu.com/
