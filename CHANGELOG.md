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
- A read-only `ai-ops doctor` command with human/JSON diagnostics for the database, packaged
  resources, scheduler safety, browser runtimes, and optional external adapters.
- A deterministic `ai-ops demo` command that exercises ingest, review, dry-run planning, a durable
  job, Fake Publisher, Fake Metrics, and final review in isolated SQLite storage. It is explicitly
  synthetic/offline, consumes no credentials, performs zero external calls, and cleans temporary
  data by default.
- Installed-wheel contract smoke for `doctor --json` and `demo --json`, including JSON parsing and
  assertions that the synthetic final review passed without external calls.
- Stable top-level `ok` and `exit_code` fields for successful and failed-review offline demo JSON.
- An Agent contract v1 vertical slice across Python, HTTP, and CLI: independent bearer principals,
  least-privilege scopes, immutable content/target/timing digests, independent human review with
  verified asset download, durable jobs, status projection, manual metrics, and performance review.
- Expiring, fenced idempotency leases for manual Agent metrics collection. Normalized snapshots are
  uniquely bound to their operation, so cancellation, process loss, response-finalization failure,
  and overlapping stale owners recover with the same key without duplicate persisted snapshots.
- Durable fixed 1h/24h/7d metrics tasks created with successful publication finalization and
  repaired by bounded database scans. Expiring fenced leases, bounded retries/concurrency, unique
  snapshot binding, and 1h/6h/24h deadline grace prevent restart loss, duplicate persisted
  snapshots, and very late current observations from being mislabeled as historical window evidence.

### Changed

- Zhihu exact Agent targets now bind a canonical public `whoami.id` identity through planning,
  human review, the plan digest, and the final pre-write check. `zhihu-login` reports the value
  for an explicit operator PATCH and never changes account data automatically.
- YouTube CLI remains available as a legacy canary, but its Agent exact renderer is paused until
  an audited read-only probe can bind each OAuth profile to the intended channel.
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
- `scripts/seed_demo.sh` is now documented as a legacy UI seed; the supported five-minute value
  path is `ai-ops doctor` followed by `ai-ops demo`.
- Worker execution dependencies now have an explicit injection boundary for isolated demos and
  tests, including rate policy, timeouts, receipts, account leases, notifications, and exception
  reporting, while the production default path remains unchanged.
- Performance review ranks metrics in the database and hydrates only the latest in-window snapshot
  per requested job; equal timestamps use the metric ID as a deterministic tie-breaker.
- Account profile serialization now includes the generic account-login endpoint and durable metrics
  reads. In the supported shared-lock topology, publishing, login, health probes, and metrics
  collection cannot concurrently mutate or consume the same profile state.
- Phase 0 was accepted on 2026-08-11 after all six CI jobs passed on merged `main@16eccb5`.

### Security

- Agent v1 now bounds raw request bodies before authentication/JSON parsing, bounds every nested DTO
  and response, keeps the legacy management key separate, and atomically arbitrates exact versus
  legacy scheduling of the same content.
- Interactive Zhihu QR login now shares the same per-account operation lease as publication, and
  exact writes fail before the article subprocess if the current login identity differs from the
  approved destination.
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
- Doctor refuses mutable SQLite recovery state and probes existing databases through an immutable
  read-only handle; PostgreSQL catalog checks run in a server-enforced read-only transaction.
- Doctor verifies every server-rendered UI template and every core table represented by the
  current migration head instead of accepting a partially damaged installation or schema.
- Explicit demo databases are built in a private sibling directory and promoted without following
  symlinks or overwriting databases and SQLite sidecars supplied by the caller.

### Known limitations

- Phase 0 remains an Alpha reliability foundation, not a high-availability scheduler. The one-worker
  topology is enforced by deployment practice rather than a distributed leader lease.
- Stale/cancelled `RUNNING` jobs are moved to an operator-reviewable failure state. A locally
  journaled structured receipt is recovered when available; otherwise the platform outcome remains
  unknown. Publish jobs still lack expiring claim leases, publisher idempotency keys, and automated
  platform-side reconciliation.
- Durable follow-up tasks cannot create evidence when a platform adapter lacks a verified post
  identity or real metrics collector; those capabilities remain absent from several adapters.
- Metrics leases fence database finalization and prevent duplicate persisted snapshots, but they do
  not provide external read exactly-once semantics: a process crash after the collector responds and
  before the transaction commits can cause the same platform metric to be read again after recovery.
- The 24-hour snapshot and account-health decision currently commit atomically, so a deterministic
  evaluator failure can retry the platform read. Health thresholds also use a moving seven-day
  median that is not yet frozen/calibrated by a long-running platform canary; a dedicated feedback
  outbox and calibrated baseline are planned before treating automatic bans as production evidence.
- Real platform behavior depends on unpinned external repositories and changing browser UIs. See
  `docs/platform-capabilities.md` for the evidence level of each adapter.
- The Zhihu CLI adapter is disabled by default: 0.2.4 lacks structured write output,
  content-file input and idempotency, and its Markdown/HTML behavior still needs real canary evidence.
- The YouTube CLI adapter is disabled by default and still requires a dedicated-channel private
  canary; unverified Google API projects cannot use it as evidence of public publication.
