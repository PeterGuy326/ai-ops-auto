from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./data/ai_ops.db"
    fernet_key: str = ""

    llm_default: Literal["openai", "anthropic", "deepseek", "dashscope"] = "openai"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_api_key: str = ""
    deepseek_api_key: str = ""
    dashscope_api_key: str = ""

    external_sau_path: Path = Path("./external/social-auto-upload")
    external_mpt_path: Path = Path("./external/MoneyPrinterTurbo")
    external_sau_url: str = ""
    external_mpt_url: str = ""
    external_xhs_mcp_url: str = ""
    mpt_api_key: str = ""  # MPT 的 x-api-key（若 config.toml 设置了 app.api_key 必填）

    scheduler_backend: Literal["apscheduler", "celery"] = "apscheduler"
    celery_broker_url: str = ""

    rate_limit_per_day: int = 5
    dedup_simhash_threshold: float = 0.85

    # ====== 风控对抗（小红书等高风控平台）======
    # 浏览器引擎：playwright_chromium / playwright_chrome_channel / patchright / camoufox
    browser_engine: str = "playwright_chrome_channel"
    # 是否无头（高风控平台建议 False，更不易被识别）
    browser_headless: bool = False
    # 代理（每账号绑定独立 IP 是反风控核心。格式：http://user:pass@host:port）
    browser_proxy: str = ""
    # 发布间隔下限（秒）— 同账号两次发布最小间隔，规避频率检测
    publish_min_interval_seconds: int = 14400  # 默认 4 小时
    # 单账号每日发布上限（小红书新号 1，养号期后 2-3 最稳）
    publish_max_per_day: int = 2
    # 养号期天数（账号注册后多少天内不发布，仅浏览/点赞）
    nurture_days: int = 7
    # 内容差异化阈值（同主题不同账号的 simhash 距离下限）
    cross_account_dedup_threshold: float = 0.6
    # 发布时间打散窗口（秒）— 计划时间 + random(0, N)，规避"整点发布"机器签名
    publish_jitter_seconds: int = 600
    # 文案是否过 humanize 反 AI 检测（默认开；调试时可关）
    xhs_humanize_enabled: bool = True

    # ====== GitHub Pages / 自有博客 ======
    # 本地 Hexo/Jekyll/Hugo 仓库路径（用户的博客源码）
    github_pages_path: Path = Path("/home/huyz/data/github/PeterGuy326.github.io")
    # 博客类型：hexo / jekyll / hugo（决定如何生成）
    github_pages_engine: str = "hexo"
    # 文章子目录（Hexo: source/_posts; Jekyll: _posts; Hugo: content/posts）
    github_pages_posts_dir: str = "source/_posts"
    # 图片子目录（相对仓库根）
    github_pages_images_dir: str = "source/img"
    # 发布前置命令（多个用 && 串联；hexo 推荐 "pnpm install --frozen-lockfile && pnpm hexo clean && pnpm hexo generate"）
    github_pages_build_cmd: str = "pnpm hexo clean && pnpm hexo generate"
    # 站点 base URL（构成 platform_url）
    github_pages_base_url: str = "https://peterguy326.github.io"
    # dry_run: True 时只渲染 markdown 预览，不写文件 / 不构建 / 不 git push（安全演练）
    github_pages_dry_run: bool = False

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_log_level: str = "info"

    data_dir: Path = Field(default=Path("./data"))

    # ====== 通知模块（Task B）======
    # 飞书 custom robot webhook（钉钉/企微留 adapters.py 空壳，out of scope, follow-up）
    feishu_webhook_url: str = ""
    # 同事件去重滑窗（秒）
    notify_dedup_window_seconds: int = 300
    # 滑窗内第 N 次聚合放行（首条 + 第 N 次 = 5 min 内最多 2 条，对齐 publishing-sop §八）
    notify_dedup_threshold: int = 3


settings = Settings()
