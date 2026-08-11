# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project intends to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once release automation is established.

## [Unreleased]

### Added

- Agent-native, China-first Creator Ops Control Plane positioning, getting-started guide,
  evidence-based platform capability matrix, and public roadmap.
- Apache-2.0 license, security policy, contribution guide, issue/PR templates, and CI gates.
- Safe-by-default background publishing switch and explicit scheduler polling/retry settings.
- Packaging declarations for server-rendered templates, prompts, and Alembic migration resources.
- A dedicated `ai-ops worker` process that owns APScheduler, periodic jobs, and bounded scans of
  due `PENDING`/`RETRYING` records from the database.
- Database-conditional job claims, persisted exponential retry times, terminal retry exhaustion,
  and article status aggregation across fan-out publish jobs.
- Bounded worker concurrency, publisher execution timeouts, scan-loop backoff, and fail-closed
  reconciliation of stale/cancelled `RUNNING` jobs without blind automatic replay.
- Signed, time-limited UI sessions and session-bound CSRF protection when `API_KEY` is configured.
- Explicit UI error states for failed control-plane queries instead of presenting empty data as a
  successful response.
- SAU login credential synchronization for current and legacy cookie layouts, with a non-secret
  account reference for upstream versions that retain login state only on disk.
- A feature-gated `pyzhihu-cli==0.2.4` article adapter with per-account HOME isolation, explicit
  QR login, online health checks, strict post-ID confirmation, and fake-CLI contract tests.
- A feature-gated `youtubeuploader v1.25.5` adapter with isolated per-account OAuth files,
  metadata files outside argv, receipt-driven video identity, and partial/unknown outcomes.
- A first-class unknown-publication outcome that stops Publisher fallback and durable retry when
  an adapter reports that an external write may have started.
- A private, redacted publish-receipt spool keyed by durable job/operation IDs. Confirmed CLI
  post IDs and URLs survive database-finalization failures and stale-worker reconciliation.
- Exact publisher-kind persistence and opt-in metrics routing; adapters without a real collector
  now return `skipped` instead of writing synthetic zero engagement or affecting account health.
- Publish and health-check paths share a kernel-backed per-account profile lease. Health probes
  skip busy accounts and discard results made stale by a concurrent publish, credential change,
  or newer health update.
- Fail-closed legacy CLI contracts for SAU and XhsSkills: process exit zero without a platform
  post identity is now unknown rather than a false successful publication.
- A source-audited CLI decision matrix covering Zhihu, Xiaohongshu, Douyin, Bilibili, WeChat,
  TikTok, YouTube, GitHub Pages, and the retained SAU compatibility paths.
- Expiring BANNED-account recovery probes: a due account remains blocked from publishing and is
  restored only after an explicit read-only health check returns HEALTHY.
- Installed-wheel/Alembic, PostgreSQL migration, and Docker runtime smoke gates in local/CI
  release verification.

### Changed

- Open-source defaults no longer contain organization-internal endpoints, personal absolute paths,
  personal site URLs, or a fixed notification chat ID.
- The only advertised scheduler backend is the implemented APScheduler worker; the unimplemented
  Celery extra/configuration promise has been removed.
- GitHub Pages publication defaults to dry-run mode.
- GitHub Pages live publication now uses fixed build/git argv, a repository-wide process lock,
  controlled/decoded image assets with size limits, exact commit-path verification, clean-worktree
  preflight, source-branch remote SHA verification, fail-closed push uncertainty, and exact-path
  rollback before a commit exists; this is not yet Pages deployment or live-URL verification.
- The API process no longer starts a scheduler. Operators run one API process and one dedicated
  worker against the same database.
- The React console no longer falls back to mock topics, fabricated create/update results, or
  silent empty lists when API requests fail.
- SAU CLI integration now uses the upstream `upload-video` and `upload-note` action names.
- Package metadata now declares the Alpha status and supported Python versions explicitly.
- Platform adapters without a reproducible, redacted evidence card are presented as Experimental,
  even when historical project notes describe a successful publish.
- Direct browser publishers preserve confirmed receipts across teardown failures; once a final
  write click starts, missing identity or post-click exceptions become non-retryable unknowns.
- Uncalibrated Baijiahao and Sohuhao selector publishers are absent from the executable registry
  unless their dedicated canary flags are enabled.
- MoneyPrinterTurbo and FunClip local CLIs now require an explicit repository-local isolated venv;
  neither falls back to the control-plane or PATH Python interpreter.
- MoneyPrinterTurbo and FunClip runs use private, unique output directories and accept only the
  current invocation's contained, non-symlink, non-empty artifacts.

### Security

- Automatic background publication is disabled by default and requires explicit operator opt-in.
- Deployment and example configuration now document credential handling and public exposure risks.
- Server-rendered `/ui` routes are protected by a login session when `API_KEY` is set; state-changing
  UI forms require CSRF tokens.
- Container startup logs no longer print `DATABASE_URL`, which can embed a database password.
- SAU subprocess output is consumed without logging potentially sensitive login or cookie data.
- External browser CLI environments now use an allowlist instead of inheriting application keys;
  SAU cookie mirrors are atomic `0600` files and task receipts omit command/content output.
- Zhihu CLI subprocesses receive a minimal environment, never receive cookies in argv, use private
  account profiles, and persist only redacted outcome metadata.
- YouTube CLI subprocesses receive a minimal environment and process group; OAuth files and
  temporary metadata/receipt files are isolated with private permissions.
- Fernet protection is explicitly scoped to the database credential blob; external CLI HOME,
  OAuth/cookie files, and browser profiles remain filesystem-protected deployment secrets.
- Video diversification now accepts an ffmpeg result only after a zero exit, non-empty temporary
  output and ffprobe video-stream check, then atomically replaces the destination.

### Known limitations

- Phase 0 remains an Alpha reliability foundation, not a high-availability scheduler. The one-worker
  topology is enforced by deployment practice rather than a distributed leader lease.
- Stale/cancelled `RUNNING` jobs are moved to an operator-reviewable failure state. A locally
  journaled structured receipt is recovered when available; otherwise the platform outcome remains
  unknown. The project still lacks claim leases, publisher idempotency keys, and automated
  platform-side reconciliation.
- Follow-up metrics callbacks are not yet durable across worker restarts, and platform post identity
  or metrics collection is absent from several adapters.
- Login and metrics paths do not yet participate in the publish/health account-operation lease;
  operators must avoid explicit login while the same account is publishing.
- Real platform behavior depends on unpinned external repositories and changing browser UIs. See
  `docs/platform-capabilities.md` for the evidence level of each adapter.
- The Zhihu CLI adapter is disabled by default: 0.2.4 lacks structured write output,
  content-file input and idempotency, and its Markdown/HTML behavior still needs real canary evidence.
- The YouTube CLI adapter is disabled by default and still requires a dedicated-channel private
  canary; unverified Google API projects cannot use it as evidence of public publication.
