import hashlib
import ipaddress
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PrincipalType = Literal["agent", "human", "service"]
SCOPE_CONTENT_STAGE = "content:stage"
SCOPE_PLAN_CREATE = "plan:create"
SCOPE_APPROVAL_REQUEST = "approval:request"
SCOPE_APPROVAL_READ = "approval:read"
SCOPE_APPROVAL_DECIDE = "approval:decide"
SCOPE_SCHEDULE_CREATE = "schedule:create"
SCOPE_JOB_READ = "job:read"
SCOPE_METRICS_COLLECT = "metrics:collect"
SCOPE_PERFORMANCE_READ = "performance:read"

AGENT_V1_SCOPES = frozenset(
    {
        SCOPE_CONTENT_STAGE,
        SCOPE_PLAN_CREATE,
        SCOPE_APPROVAL_REQUEST,
        SCOPE_APPROVAL_READ,
        SCOPE_APPROVAL_DECIDE,
        SCOPE_SCHEDULE_CREATE,
        SCOPE_JOB_READ,
        SCOPE_METRICS_COLLECT,
        SCOPE_PERFORMANCE_READ,
    }
)

# Backward-readable name used at the policy check sites.
APPROVAL_DECIDE_SCOPE = SCOPE_APPROVAL_DECIDE
HUMAN_APPROVAL_SCOPES = frozenset({SCOPE_APPROVAL_READ, SCOPE_APPROVAL_DECIDE})

# Collection leases also cover validation, database fencing, and commit after
# the bounded external call returns.
EXTERNAL_OPERATION_FINALIZE_MARGIN_SECONDS = 30


class AgentPrincipalConfig(BaseModel):
    """One pre-provisioned bearer principal.

    Only a SHA-256 verifier is accepted.  Raw bearer tokens must stay in the
    caller's secret store and are never part of application configuration.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    type: PrincipalType
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scopes: tuple[str, ...] = ()

    @field_validator("principal_id")
    @classmethod
    def _validate_principal_id(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("principal_id must not contain surrounding whitespace")
        return value

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not scope or scope != scope.strip() for scope in values):
            raise ValueError("scopes must be non-empty and contain no surrounding whitespace")
        if len(set(values)) != len(values):
            raise ValueError("scopes must be unique within a principal")
        unknown = set(values) - AGENT_V1_SCOPES
        if unknown:
            raise ValueError("scopes contain an unknown Agent contract v1 scope")
        return values

    @model_validator(mode="after")
    def _restrict_approval_decision(self):
        if self.type != "human" and HUMAN_APPROVAL_SCOPES.intersection(self.scopes):
            raise ValueError("only human principals may receive approval read/decide scopes")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    database_url: str = "sqlite:///./data/ai_ops.db"
    fernet_key: str = ""

    # DeepSeek/DashScope can still be used through their OpenAI-compatible
    # endpoints by setting OPENAI_BASE_URL/OPENAI_MODEL. They are deliberately
    # not advertised as separate drivers until they have distinct contracts.
    llm_default: Literal["openai", "anthropic", "claude_cli"] = "openai"
    openai_api_key: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    # 默认对话模型；使用 OpenAI 兼容网关时可同时覆盖
    # OPENAI_BASE_URL 与 OPENAI_MODEL。
    openai_model: str = "gpt-4o-mini"
    anthropic_api_key: str = ""

    # ====== 本地 Claude Code 作为 LLM 后端（LLM_DEFAULT=claude_cli）======
    # 走本机 `claude -p` headless，复用已登录的 Claude Code 鉴权/额度，
    # 无需单独 OpenAI/Anthropic key，简历数据不流向第三方。
    claude_cli_bin: str = "claude"  # claude 可执行文件（不在 PATH 时填绝对路径）
    claude_cli_model: str = ""  # 空=用 Claude Code 默认模型；可填 sonnet/opus/haiku
    claude_cli_timeout_seconds: int = 120  # 单次 subprocess 超时（兜底防卡死）

    # ====== AI 短剧视频 · HappyHorse（DashScope 异步协议）======
    # 这是可选的私有/兼容服务适配器；开源默认不配置任何组织端点。
    wukong_api_key: str = ""
    wukong_video_model: str = "happyhorse-1.0-t2v"
    # 配置项应指向你自己有权使用的 jobs 端点；留空表示未配置。
    wukong_video_jobs_url: str = ""
    wukong_video_resolution: str = "720P"
    wukong_video_ratio: str = "9:16"  # 竖屏短剧
    wukong_timeout_seconds: int = 1800
    wukong_poll_interval_seconds: int = 15
    wukong_download: bool = True

    external_sau_path: Path = Path("./external/social-auto-upload")
    external_mpt_path: Path = Path("./external/MoneyPrinterTurbo")
    # CLI 模式必须显式使用 MPT 仓库内的独立 venv；空值会 fail closed，
    # 绝不回退 PATH 中的 python（避免误用控制面解释器及其 site-packages）。
    mpt_python: str = ""
    external_sau_url: str = ""
    sau_cli_timeout_seconds: int = Field(default=1500, ge=1, le=7200)
    external_mpt_url: str = ""
    external_xhs_mcp_url: str = ""
    mpt_api_key: str = ""  # MPT 的 x-api-key（若 config.toml 设置了 app.api_key 必填）
    mpt_cli_timeout_seconds: int = Field(default=1800, ge=1, le=7200)

    # ====== 知乎 CLI（第三方 BAIGUANGMEI/zhihu-cli）======
    # CLI 是软依赖；启用但未安装时会安全回退到浏览器 Publisher。
    # 默认关闭：0.2.4 只有源码级契约证据，完成专用账号 canary 后再显式开启。
    zhihu_cli_enabled: bool = False
    zhihu_cli_bin: str = "zhihu"
    zhihu_cli_timeout_seconds: int = Field(default=300, ge=1, le=1800)
    # 上游把登录态固定写在 $HOME/.zhihu-cli。为避免多个账号串号，本项目给
    # 每个 account_id 注入独立 HOME：<root>/account_<id>/.zhihu-cli。
    zhihu_cli_profile_root: Path = Path("./data/cli_profiles/zhihu")
    zhihu_cli_asset_root: Path = Path("./data")
    # 当前上游只收 positional argv，不支持 stdin/--content-file。限制正文大小，
    # 避免撞操作系统 ARG_MAX；超限会在写操作前安全回退浏览器链路。
    zhihu_cli_max_content_bytes: int = Field(default=60_000, ge=1024, le=131_072)
    zhihu_cli_max_image_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    zhihu_cli_max_total_image_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)

    # ====== YouTube uploader CLI（porjo/youtubeuploader v1.25.5）======
    youtube_uploader_enabled: bool = False
    youtube_uploader_bin: str = "youtubeuploader"
    # 必须小于 worker 的全局执行超时，给子进程回收与 DB 落库留窗口。
    youtube_uploader_timeout_seconds: int = Field(default=1500, ge=1, le=7200)
    youtube_uploader_profile_root: Path = Path("./data/cli_profiles/youtube")
    youtube_uploader_asset_root: Path = Path("./data")
    youtube_uploader_max_video_bytes: int = Field(
        default=20 * 1024 * 1024 * 1024,
        ge=1024,
    )

    # 当前唯一实现的调度后端。保留字段是为了让非法值启动时立即失败，
    # 不代表 Celery/Redis 已可用。
    scheduler_backend: Literal["apscheduler"] = "apscheduler"
    scheduler_timezone: str = "Asia/Shanghai"
    scheduler_poll_seconds: int = Field(default=15, ge=1)
    scheduler_max_concurrency: int = Field(default=4, ge=1, le=100)
    job_retry_base_seconds: int = Field(default=60, ge=1)
    # Post-publication 1h/24h/7d reads use their own durable task ledger. The
    # owner lease must outlive the bounded collector call.
    metrics_task_collection_timeout_seconds: int = Field(default=120, ge=1, le=1800)
    metrics_task_lease_seconds: int = Field(default=300, ge=2, le=7200)
    metrics_task_max_attempts: int = Field(default=5, ge=1, le=20)
    metrics_task_retry_base_seconds: int = Field(default=300, ge=1, le=86400)
    metrics_task_max_concurrency: int = Field(default=4, ge=1, le=100)
    # Metrics never wait behind a long publish/login operation while occupying
    # a bounded scanner slot; a busy row is durably deferred without an attempt.
    metrics_task_account_lock_timeout_seconds: int = Field(default=1, ge=0, le=60)
    # Publisher hard timeout is lower than stale RUNNING reconciliation so an
    # active execution cannot be mistaken for an abandoned one.
    job_execution_timeout_seconds: int = Field(default=1800, ge=1)
    job_running_timeout_seconds: int = Field(default=7200, ge=1)
    # Publish waits for a short health/profile operation through a kernel-backed
    # per-account lease; health probes use non-blocking acquisition and skip.
    account_operation_lock_timeout_seconds: int = Field(default=120, ge=1, le=1800)
    health_probe_timeout_seconds: int = Field(default=60, ge=1, le=600)
    # 安全默认：审核/分发可以创建持久任务，但后台不会自动执行真发布。
    # 只有运营者显式设置 AUTO_PUBLISH_ENABLED=true 才开启自动扫描/执行。
    auto_publish_enabled: bool = False

    @model_validator(mode="after")
    def _validate_scheduler_timeouts(self):
        if self.job_running_timeout_seconds <= self.job_execution_timeout_seconds:
            raise ValueError(
                "JOB_RUNNING_TIMEOUT_SECONDS must be greater than JOB_EXECUTION_TIMEOUT_SECONDS"
            )
        if (
            self.youtube_uploader_enabled
            and self.youtube_uploader_timeout_seconds >= self.job_execution_timeout_seconds
        ):
            raise ValueError(
                "YOUTUBE_UPLOADER_TIMEOUT_SECONDS must be lower than JOB_EXECUTION_TIMEOUT_SECONDS"
            )
        if (
            self.metrics_task_lease_seconds
            <= self.metrics_task_collection_timeout_seconds
            + EXTERNAL_OPERATION_FINALIZE_MARGIN_SECONDS
        ):
            raise ValueError(
                "METRICS_TASK_LEASE_SECONDS must be greater than "
                "METRICS_TASK_COLLECTION_TIMEOUT_SECONDS plus the 30-second "
                "finalization margin"
            )
        return self

    # 64-bit simhash Hamming distance. A result below this integer is blocked.
    simhash_hamming_threshold: int = Field(default=8, ge=0, le=64)

    # ====== 浏览器兼容性与运营安全 ======
    # 浏览器引擎：playwright_chromium / playwright_chrome_channel / patchright / camoufox
    browser_engine: str = "playwright_chrome_channel"
    # 是否无头；部分平台登录流程只在 headed 模式可操作。
    browser_headless: bool = False
    # 可选网络代理。仅使用有权访问的代理并遵守平台条款。
    # 格式：http://user:pass@host:port
    browser_proxy: str = ""
    # CDP 远程调试端点：配置后连接操作者明确授权的现有 Chrome 会话。
    # 只连接受信任的本机端点；CDP 等同于浏览器控制权限。
    # 形如 "http://127.0.0.1:9333"；留空则由项目启动独立浏览器。
    browser_cdp_url: str = ""
    # 发布间隔下限（秒）— 限制操作频率和事故半径。
    publish_min_interval_seconds: int = Field(
        default=14400,
        ge=60,
        le=7 * 24 * 60 * 60,
    )  # 默认 4 小时
    # 单账号每日发布上限；真实值应按平台条款和运营政策收紧。
    publish_max_per_day: int = Field(default=2, ge=1, le=50)
    # 新账号进入自动发布前的人工观察期。
    nurture_days: int = Field(default=7, ge=0, le=365)
    # 排程抖动窗口（秒）— 分散同时到期任务，降低瞬时资源峰值。
    publish_jitter_seconds: int = Field(default=600, ge=0, le=24 * 60 * 60)
    # 是否执行可读性/风格整理（默认开；调试时可关）。
    xhs_humanize_enabled: bool = True

    # Stub browser publishers contain selectors that have not completed a
    # dedicated-account canary. Keep them out of the executable registry until
    # an operator explicitly opts into that risk.
    baijiahao_publisher_enabled: bool = False
    sohuhao_publisher_enabled: bool = False

    # ====== GitHub Pages / 自有博客 ======
    # 本地 Hexo/Jekyll/Hugo 仓库路径（用户的博客源码）
    github_pages_path: Path = Path("./external/site")
    # 博客类型：当前 Publisher 仅实现 hexo；其他值会在写入前拒绝。
    github_pages_engine: str = "hexo"
    # 文章子目录（Hexo: source/_posts; Jekyll: _posts; Hugo: content/posts）
    github_pages_posts_dir: str = "source/_posts"
    # 图片子目录（相对仓库根）
    github_pages_images_dir: str = "source/img"
    # 只有该目录内通过真实图片解码校验的文件才允许复制到公开站点。
    github_pages_asset_root: Path = Path("./data/assets")
    github_pages_max_image_bytes: int = Field(
        default=20 * 1024 * 1024,
        ge=1024,
        le=100 * 1024 * 1024,
    )
    github_pages_max_total_image_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1024,
        le=500 * 1024 * 1024,
    )
    # Hexo 构建工具。只允许固定 argv 模板，不接受 shell 命令字符串。
    github_pages_build_tool: Literal["pnpm", "npx"] = "pnpm"
    github_pages_build_timeout_seconds: int = Field(default=600, ge=1, le=3600)
    github_pages_git_timeout_seconds: int = Field(default=120, ge=1, le=600)
    # 等待同一博客仓库中另一个 live 发布完成的最长时间。
    github_pages_lock_timeout_seconds: int = Field(default=900, ge=1, le=7200)
    github_pages_remote: str = "origin"
    github_pages_branch: str = "main"
    # 站点 base URL（构成 platform_url）
    github_pages_base_url: str = ""
    # dry_run: True 时只渲染 markdown 预览，不写文件 / 不构建 / 不 git push（安全演练）
    github_pages_dry_run: bool = True

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_log_level: str = "info"

    data_dir: Path = Field(default=Path("./data"))

    # Agent contract v1 accepts only pre-staged files under this import root,
    # then copies them into a separate content-addressed vault before approval.
    agent_asset_import_root: Path = Path("./data/agent-import")
    agent_asset_vault_root: Path = Path("./data/agent-vault")
    agent_asset_max_bytes: int = Field(
        default=512 * 1024 * 1024,
        ge=1,
        le=50 * 1024 * 1024 * 1024,
    )
    agent_asset_max_total_bytes: int = Field(
        default=2 * 1024 * 1024 * 1024,
        ge=1,
        le=100 * 1024 * 1024 * 1024,
    )
    # Manual Agent metrics reads run outside the database transaction.  Bound
    # them below a durable operation lease so cancellation/crash recovery can
    # safely reclaim the same idempotency key.
    agent_metrics_collection_timeout_seconds: int = Field(default=120, ge=1, le=1800)
    agent_external_operation_lease_seconds: int = Field(default=300, ge=2, le=7200)

    # ====== 通知模块（Task B）======
    # 飞书 custom robot webhook（钉钉/企微留 adapters.py 空壳，out of scope, follow-up）
    feishu_webhook_url: str = ""
    # 同事件去重滑窗（秒）
    notify_dedup_window_seconds: int = 300
    # 滑窗内第 N 次聚合放行（首条 + 第 N 次 = 5 min 内最多 2 条，对齐 publishing-sop §八）
    notify_dedup_threshold: int = 3

    # ====== Task F · API 鉴权 ======
    # 写操作 / 敏感读路由的 X-API-Key 校验值。空字符串默认 fail closed；
    # 生产部署必须从环境注入非空值。
    api_key: str = ""
    # Legacy 匿名模式必须二次显式开启；默认空 API_KEY 现在是 fail closed。
    # 该开关只用于 loopback 本地开发，`ai-ops serve` 会拒绝把它绑定到非
    # loopback 地址。
    legacy_dev_auth_bypass: bool = False

    # ====== Agent contract v1 · independent bearer principals ======
    # JSON array supplied through AGENT_PRINCIPALS.  Entries contain only a
    # SHA-256 token verifier, never the raw bearer token.  This identity plane
    # is deliberately independent from the legacy X-API-Key/dev-mode contract.
    agent_principals: list[AgentPrincipalConfig] = Field(default_factory=list)

    @field_validator("api_key")
    @classmethod
    def _validate_legacy_api_key(cls, value: str) -> str:
        if not value:
            return value
        if (
            len(value) < 32
            or value != value.strip()
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
        ):
            raise ValueError("API_KEY must be empty or at least 32 printable ASCII characters")
        return value

    @model_validator(mode="after")
    def _validate_legacy_dev_bind(self):
        if not self.legacy_dev_auth_bypass:
            return self
        normalized = self.api_host.strip().strip("[]").lower()
        try:
            loopback = normalized == "localhost" or ipaddress.ip_address(normalized).is_loopback
        except ValueError:
            loopback = normalized == "localhost"
        if not loopback:
            raise ValueError("LEGACY_DEV_AUTH_BYPASS requires API_HOST to be a loopback address")
        return self

    @model_validator(mode="after")
    def _validate_agent_asset_root_separation(self):
        try:
            import_root = self.agent_asset_import_root.expanduser().resolve(strict=False)
            vault_root = self.agent_asset_vault_root.expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            raise ValueError("Agent asset roots must be resolvable paths") from None

        if (
            import_root == vault_root
            or import_root.is_relative_to(vault_root)
            or vault_root.is_relative_to(import_root)
        ):
            raise ValueError(
                "AGENT_ASSET_IMPORT_ROOT and AGENT_ASSET_VAULT_ROOT must be "
                "separate, non-overlapping directories"
            )
        return self

    @model_validator(mode="after")
    def _validate_agent_asset_limits(self):
        if self.agent_asset_max_total_bytes < self.agent_asset_max_bytes:
            raise ValueError("AGENT_ASSET_MAX_TOTAL_BYTES must be at least AGENT_ASSET_MAX_BYTES")
        return self

    @model_validator(mode="after")
    def _validate_agent_external_operation_timeouts(self):
        if (
            self.agent_external_operation_lease_seconds
            <= self.agent_metrics_collection_timeout_seconds
            + EXTERNAL_OPERATION_FINALIZE_MARGIN_SECONDS
        ):
            raise ValueError(
                "AGENT_EXTERNAL_OPERATION_LEASE_SECONDS must be greater than "
                "AGENT_METRICS_COLLECTION_TIMEOUT_SECONDS plus the 30-second "
                "finalization margin"
            )
        return self

    @model_validator(mode="after")
    def _validate_agent_principal_uniqueness(self):
        principal_ids: set[str] = set()
        token_hashes: set[str] = set()
        legacy_api_key_hash = (
            hashlib.sha256(self.api_key.encode("utf-8")).hexdigest() if self.api_key else None
        )
        for principal in self.agent_principals:
            if principal.principal_id in principal_ids:
                raise ValueError("AGENT_PRINCIPALS principal_id values must be unique")
            if principal.token_sha256 in token_hashes:
                raise ValueError("AGENT_PRINCIPALS token_sha256 values must be unique")
            if legacy_api_key_hash is not None and principal.token_sha256 == legacy_api_key_hash:
                raise ValueError("Agent bearer tokens must not reuse the legacy API_KEY")
            principal_ids.add(principal.principal_id)
            token_hashes.add(principal.token_sha256)
        return self

    # ====== Task G · 可观测性 ======
    # Sentry DSN；空 = 不启用 Sentry（sentry-sdk 为软依赖，未装也不报错）
    sentry_dsn: str = ""
    # Sentry environment 标签（dev / staging / prod）
    sentry_environment: str = "dev"
    # 日志格式：text = 人类可读（默认，本地调试友好）；json = 结构化（生产推荐，配 ELK/Datadog）
    log_format: Literal["text", "json"] = "text"
    # 日志级别：DEBUG / INFO / WARNING / ERROR
    log_level: str = "INFO"

    # ====== Task · notify lark-cli 后端（双后端架构）======
    # notify 后端切换：
    #   "lark_cli" = 只走 lark-cli OpenAPI（需本机 lark-cli auth login + scope:im:message）
    #   "webhook"  = 只走飞书 custom robot webhook（需 FEISHU_WEBHOOK_URL）
    #   "both"     = 两路并发尝试，任一成功即视为 success（dev 默认，零配置即用）
    # 底层逻辑：dev 用 cli 零配置，prod 用 webhook 解耦人机依赖，迁移期 both 兜底
    notify_backend: str = "both"
    # lark-cli 目标群（多个用逗号分隔）。开源默认留空，避免误发。
    # 用 str + 运行时 split，避免 pydantic-settings 对 list[str] 的 env JSON 解析坑。
    lark_cli_chat_ids: str = ""
    # lark-cli subprocess 总超时（秒）—— 兜底防 cli 本身卡死拖垮主业务
    lark_cli_timeout_seconds: int = 15

    # ====== Video Clipper · FunClip（智能视频剪辑，阿里达摩院/ModelScope 开源）======
    # 外置 FunClip 仓库路径（git clone https://github.com/modelscope/FunClip）
    funclip_path: Path = Path("./external/FunClip")
    # FunClip 专用 venv 的 python（必须在 FUNCLIP_PATH 内且有 pyvenv.cfg）。
    # 空值 fail closed，绝不回退控制面 sys.executable/PATH python。
    funclip_python: str = ""
    # subprocess 超时（秒）—— ASR + 剪辑都受这个上限管，长视频转写慢，默认 30 min
    funclip_timeout_seconds: int = 1800
    # 默认输出根目录（每次调用会在下面建 run_<ts>/ 子目录隔离产物）
    funclip_output_root: Path = Path("./data/clips")

    # ====== AI 短剧 · 可灵 Kling（云视频生成，快手；本地零算力）======
    # 鉴权走 JWT(HS256)：iss=access_key，用 secret_key 签名，token 30min 过期。
    kling_access_key: str = ""
    kling_secret_key: str = ""
    # 区域域名：api.klingai.com / api-beijing.klingai.com / api-singapore.klingai.com
    kling_api_base: str = "https://api.klingai.com"
    kling_model: str = "kling-v2-6"
    # 生成清晰度档：std（性价比）/ pro（高画质）
    kling_mode: str = "pro"
    # 异步任务总超时 + 轮询间隔（秒）
    kling_timeout_seconds: int = 1800
    kling_poll_interval_seconds: int = 5
    # 成片是否下载到本地（发布器要本地文件；Kling 生成物 30 天后清理，建议转存）
    kling_download: bool = True

    # ====== AI 播客 · ListenHub（云播客生成，ListenHub/Marswave）======
    listenhub_api_key: str = ""
    listenhub_api_base: str = "https://api.marswave.ai/openapi"
    listenhub_timeout_seconds: int = 1800
    # 文档建议首轮等 60s 再以 10s 间隔轮询
    listenhub_poll_initial_seconds: int = 60
    listenhub_poll_interval_seconds: int = 10
    # 音频是否下载到本地（投流到视频平台时需要）
    listenhub_download: bool = True

    # ====== Round 5 · schema 漂移自检 ======
    # 应用进程内是否在 lifespan startup 自动跑 alembic upgrade head。
    # 生产默认 False —— prod 走 Dockerfile entrypoint 的 subprocess alembic upgrade
    # （已稳定），应用进程不该擅自动 schema（会绕过运维审批 + 多进程并发竞争）。
    # dev 可设 AUTO_UPGRADE_DB=true 让本地 uvicorn 启动期自愈，避免开发者
    # git pull 拿到新 model 后启动直接炸（Round 5 事故重现）。
    auto_upgrade_db: bool = False


settings = Settings()
