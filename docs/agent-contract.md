# Agent contract v1

Agent contract v1 is the narrow control-plane API for Codex, Claude, OpenClaw,
and custom agents. It is separate from the legacy administrator API: bearer
credentials never inherit the empty-`API_KEY` development bypass and cannot be
used as `X-API-Key` unless an operator has incorrectly reused the same secret
(configuration validation rejects that reuse).

## Safety flow

```text
stage_content
  -> plan_publication (exact content digest + accounts + UTC time)
  -> request_approval
  -> get_approval (separate human principal reviews the exact snapshot)
  -> download_approval_asset (human retrieves and verifies each bound asset)
  -> decide_approval (same plan digest, separate human principal)
  -> schedule (digest and account policy rechecked)
  -> durable PublishJob rows
  -> get_job_status / collect_metrics / review_performance
```

Planning comes before approval because a useful decision must bind the exact
content, assets, destination accounts, platforms, and execution time. The
target also binds the exact Publisher kind, renderer identity, renderer
contract/adapter versions, and a credential-free, host-path-free platform
payload projection plus its canonical digest. Renderers that require an
externally observable destination also bind a public stable
`approved_external_account_id`; it is returned in both planning and human
review and is covered by `plan_digest`. The approver first reads an
immutable, credential-free review bundle, then sends
its `plan_digest` back as the decision's required `expected_plan_digest`. The
service recomputes the bound snapshot when deciding and fails closed if either
digest no longer matches. Editing any bound field after approval also makes
`schedule` fail closed. Scheduling is idempotent at both the operation ledger
and database constraint: one plan can create at most one job for each account.

`schedule` only creates durable `PENDING` jobs. It does not call a publisher.
The dedicated worker still observes `AUTO_PUBLISH_ENABLED`; the default remains
`false`.

## Asset vault and immutable snapshots

`StageContentRequest.assets[].local_path` is a server-side import reference, not
an arbitrary host path or an upload URL. Configure these deployment values:

| Variable | Purpose |
|---|---|
| `AGENT_ASSET_IMPORT_ROOT` | Controlled inbox from which `/v1/contents` may read |
| `AGENT_ASSET_VAULT_ROOT` | Private, persistent content-addressed storage shared by API and worker |
| `AGENT_ASSET_MAX_BYTES` | Per-file streaming limit; default 512 MiB |
| `AGENT_ASSET_MAX_TOTAL_BYTES` | Aggregate asset limit for one content snapshot; default 2 GiB and must be at least the per-file limit |
| `AGENT_METRICS_COLLECTION_TIMEOUT_SECONDS` | Manual collection timeout; default 120 seconds |
| `AGENT_EXTERNAL_OPERATION_LEASE_SECONDS` | Durable external-operation lease; default 300 seconds and must exceed the manual collection timeout plus the 30-second finalization margin |
| `METRICS_TASK_COLLECTION_TIMEOUT_SECONDS` | Automatic fixed-window collection timeout; default 120 seconds |
| `METRICS_TASK_LEASE_SECONDS` | Automatic task owner lease; default 300 seconds and must exceed its collection timeout plus the 30-second finalization margin |
| `METRICS_TASK_ACCOUNT_LOCK_TIMEOUT_SECONDS` | Short pre-claim account-lock wait for automatic metrics; default 1 second, then durably deferred without consuming an attempt |
| `METRICS_TASK_MAX_ATTEMPTS` | Maximum real collector calls per fixed window; default 5 |
| `METRICS_TASK_RETRY_BASE_SECONDS` | Exponential retry base; default 300 seconds |
| `METRICS_TASK_MAX_CONCURRENCY` | Independent automatic collection concurrency; default 4 |

The roots must be separate and non-overlapping. The API resolves each source
strictly below the import root, rejects traversal, symlinks, directories,
devices, and oversized files, then streams it into a SHA-256-addressed vault
path using an atomic no-overwrite commit. A short lowercase safe extension is
kept on the private storage filename so CLI adapters can validate media and is
also exposed as separate `storage_suffix` review metadata. The public identity
remains `vault://sha256/<digest>`. The suffix is execution-relevant, so it is
covered by `content_digest`; changing only the suffix invalidates approval.
Equal bytes with the same normalized extension reuse the existing file; an
existing path is verified rather than replaced. Legacy Asset rows with no
complete vault metadata cannot enter the v1 approval path.

Planning persists a private `content_snapshot` containing the exact text,
ordered asset identities, safe metadata, hashes, and sizes covered by approval.
Human review exposes only `vault://sha256/<digest>`, never the host path.
An approver with `approval:read` can resolve an asset from that exact approval
through the binary download endpoint; the server revalidates the snapshot and
vault bytes, then streams from the same already-open file descriptor instead
of reopening the path after verification. Byte-range requests are rejected
with HTTP 416; resumable/partial review downloads are intentionally outside v1.
The response is `application/octet-stream`
with `Content-Length` and `X-Content-SHA256`, plus `Cache-Control: no-store` and
`X-Content-Type-Options: nosniff`. No host path or credential is returned.
Decision, scheduling, and worker execution revalidate the snapshot and vaulted
bytes; v1 jobs publish from that snapshot instead of rebuilding payloads from a
later mutable Article row. The vault is therefore durable application data:
mount it into both API and worker, restrict it from Agent write access, and back
it up consistently with the database.

## Exact renderer binding

The control-plane contract and the platform CLI are different layers. A v1
publication target is accepted only when an enabled Publisher exposes a pure,
versioned renderer that can project the approved snapshot without credentials
or host paths. Its `execution` object contains:

- renderer ID, contract version, adapter version, platform, and Publisher kind;
- accepted extra fields, tag policy, and asset-count rules;
- the final path-free payload projection (asset references are ordered slots);
- `payload_digest`, the canonical SHA-256 of the renderer contract and payload.

Both `plan_publication` and `get_approval` return this binding, and the complete
target enters `plan_digest`. Before an exact job calls an external Publisher,
the worker selects only the approved Publisher kind and recomputes the renderer
material. A missing/ambiguous Publisher, renderer error, version drift, or
payload digest mismatch is non-retryable and fail closed. It never falls back
to a heterogeneous Publisher for a v1 exact job.

The currently enabled exact renderer is deliberately narrow:

| Platform path | Renderer contract | Accepted v1 content |
|---|---|---|
| Zhihu CLI (enabled with `ZHIHU_CLI_ENABLED=true`) | `zhihu-cli.article-argv`, `4+python-markdown-3.10.3+account-id+bounds-v1+media-preflight-v1`, adapter `0.2.4` | image-text/long article, 0–9 verified JPEG images, no tags; image suffix/bytes and configured per-image/aggregate limits are checked before planning, rendered HTML obeys `ZHIHU_CLI_MAX_CONTENT_BYTES`, topic IDs are unique positive 1–32 digit values (at most 20), and Markdown 3.10.3 plus a stable `whoami.id` destination are approval-bound |
| YouTube CLI (enabled with `YOUTUBE_UPLOADER_ENABLED=true`) | exact renderer paused; legacy canary only | no v1 exact content until an audited read-only channel identity probe can bind the OAuth profile |

For Zhihu exact execution, run `ai-ops zhihu-login <account_id>` in a trusted
terminal. On successful online verification the command prints a canonical
public identity such as `zhihu:id:<whoami.id>`. An operator must place that
exact value in `Account.profile["external_account_id"]` by sending
`{"external_account_id":"zhihu:id:<whoami.id>"}` to
`PATCH /accounts/<account_id>`; the login command deliberately never writes
the database. An empty string clears the binding. Planning rejects a missing
or malformed value. Immediately before
the article write, while the worker's account operation lease is held, the
adapter runs `whoami --json`, normalizes the stable `id`, and compares it with
the approved hidden execution value. A missing or different identity fails
before write without fallback or retry.

All other platform Publishers, including browser and SAU paths, remain outside
the v1 exact execution contract and fail at planning with
`exact_renderer_unavailable`. Content outside either renderer's audited
shape fails with `content_not_supported_by_renderer`. Legacy administrator
jobs keep their existing routing behavior; this fail-closed rule is specific
to v1 approved jobs. Exact-renderer support is not a Stable-platform claim.

For v1 jobs, `planned_for` is an immutable approval-bound not-before time. The
job stores that value separately as `approved_planned_for`; `scheduled_at`
remains the mutable next-attempt time used by retry backoff and policy
deferrals. Both the due scanner and the atomic worker claim reject an exact job
before its approved time, including calls through the legacy
`POST /jobs/{id}/run` endpoint. Retrying may move `scheduled_at` without
invalidating the approval. Legacy republish is also denied for any job with a
`plan_id`: publishing it again requires a new plan and independent approval.

## Principals and scopes

`AGENT_PRINCIPALS` is a JSON array. A record contains a stable ID, a declared
type, the lowercase SHA-256 verifier of a bearer token, and explicit scopes.
The raw token belongs in the caller's secret store and must not be put in this
configuration.

Generate each token/verifier pair locally:

```bash
ai-ops gen-principal-token
```

The command emits one JSON document containing the raw token and its verifier.
Move the raw token into the caller's secret store immediately; only copy
`token_sha256` into `AGENT_PRINCIPALS`. Generate separate pairs for the Agent
and approver.

| Scope | Operation |
|---|---|
| `content:stage` | `stage_content` |
| `plan:create` | `plan_publication` |
| `approval:request` | `request_approval` |
| `approval:read` | `get_approval`, `download_approval_asset`; only `type=human` is accepted |
| `approval:decide` | `decide_approval`; only `type=human` is accepted |
| `schedule:create` | `schedule` |
| `job:read` | `get_job_status` |
| `metrics:collect` | `collect_metrics` |
| `performance:read` | `review_performance` |

Example shape (replace the placeholder hashes with generated verifiers):

```json
[
  {
    "principal_id": "creator-agent",
    "type": "agent",
    "token_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "scopes": [
      "content:stage",
      "plan:create",
      "approval:request",
      "schedule:create",
      "job:read",
      "metrics:collect",
      "performance:read"
    ]
  },
  {
    "principal_id": "publisher-approver",
    "type": "human",
    "token_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "scopes": ["approval:read", "approval:decide"]
  }
]
```

Set the compact JSON document as `AGENT_PRINCIPALS` through the deployment
secret/configuration system. Principal IDs and token hashes must be unique;
unknown scopes, duplicate scopes, non-human approval read/decision scopes, and
reuse of the legacy `API_KEY` all stop configuration loading.

The `human` type is a cryptographically separate role assertion, not biometric
proof. Keep that token in an interactive approval system, hardware-backed
secret store, SSO gateway, or another boundary the Agent cannot access. If an
Agent can read the human token, no application-level label can restore the
human guarantee.

## HTTP operations

All requests use `Authorization: Bearer <token>`. Mutating operations also use
an `Idempotency-Key` of 8–128 URL-safe characters.

| Method and path | Contract operation |
|---|---|
| `POST /v1/contents` | `stage_content` |
| `POST /v1/publication-plans` | `plan_publication` |
| `POST /v1/approval-requests` | `request_approval` |
| `GET /v1/approval-requests/{approval_id}` | `get_approval` |
| `GET /v1/approval-requests/{approval_id}/assets/{asset_id}` | `download_approval_asset` (binary) |
| `POST /v1/approvals/{approval_id}/decision` | `decide_approval` |
| `POST /v1/publication-plans/{plan_id}/schedule` | `schedule` |
| `GET /v1/jobs/{job_id}` | `get_job_status` |
| `POST /v1/jobs/{job_id}/metrics-collections` | `collect_metrics` |
| `POST /v1/performance-reviews` | `review_performance` |

Every JSON request and response DTO has `schema_version: 1` and rejects unknown
fields; the approval-asset route is the single binary response. `get_approval`
returns the exact content snapshot; each asset's vaulted
logical path, SHA-256, byte size, normalized storage suffix, and safe metadata;
target platform, account ID, account display name, credential-free
`account_binding_digest`, public `approved_external_account_id` when required,
and exact `execution` renderer binding;
`planned_for`; the bound digests; and approval
expiry. The execution projection is the deliberately public, path-free payload
covered by approval; it is not a publisher `raw_response`. The API never
returns account credentials, unfiltered adapter responses, or host filesystem
paths. All other contract responses also omit credentials and publisher
`raw_response`. Uncertain publish outcomes
explicitly require reconciliation; metrics use a fixed
collected/skipped/unavailable result rather than returning an adapter's
arbitrary dictionary.

The v1 DTOs also enforce transport-independent work limits. A staged body is
at most 1 MiB in UTF-8, `extra` is at most 64 KiB of canonical JSON with depth
8 and 1,024 aggregate object/list items, and each asset `meta` is at most
16 KiB with depth 8 and 256 items. Planning requires 1–16 unique, positive
`account_ids`; omission and an empty list are validation errors. v1 never
expands a plan to staged/default/all platform accounts, and plan, review, and
schedule responses are consequently bounded to 16 targets/jobs.

The same limits define a compact UTF-8 JSON transport envelope: every
body-bearing `/v1` operation rejects a stream above 13 MiB before bearer
authentication or JSON parsing, and an oversized declared `Content-Length` is
rejected at the route boundary. The CLI applies that identical ceiling to
file/stdin input. Each exact renderer projection is capped at 256 KiB; the
official client accepts contract JSON responses up to the derived 17 MiB
ceiling while retaining a much smaller error body limit. These are wire-format
limits, so JSON inflated with arbitrary whitespace or alternate escape
spellings can be rejected even when its decoded value would otherwise fit.
Legacy DRAFT rows are revalidated against the same body, `extra`, asset-count,
and per-asset metadata bounds before they can enter v1 planning.

Python reviewers can independently recompute the bundle before deciding:
`approval_content_digest(review.content)` must equal `review.content_digest`,
and `plan_digest(content_digest=..., targets=review.targets,
planned_for=review.planned_for)` must equal `review.plan_digest`.

An approval decision body must include the digest that the human actually
reviewed:

```json
{
  "schema_version": 1,
  "expected_plan_digest": "<plan_digest returned by get_approval>",
  "decision": "approved",
  "reason": "Reviewed content, assets, targets, and schedule"
}
```

The service compares `expected_plan_digest` with the requested approval and
recomputes the content/plan snapshot before recording the decision. A stale or
mutated subject is rejected; the caller must fetch and review the current
bundle rather than blindly retrying the decision.

Stable domain errors use this shape:

```json
{
  "schema_version": 1,
  "error": {
    "code": "approval_subject_changed",
    "message": "Content, assets, targets, or timing changed after planning"
  }
}
```

Reusing an idempotency key with the same principal, operation, and request
returns the recorded response. Reusing it with a different request returns
HTTP 409. Keys are scoped to the authenticated principal and operation. Manual
metrics collection also binds its normalized `Metrics` row to an expiring,
owned operation lease: cancellation makes the lease immediately reclaimable,
a process loss becomes reclaimable after expiry, and a retry after response
finalization failure reuses the persisted snapshot instead of calling the
external collector again. This guarantees one persisted snapshot, not one
platform read: if the process stops after the collector responds but before the
database transaction commits, recovery can invoke the collector again.

Performance review applies its optional half-open time window in the database
and returns only the latest snapshot per requested job. Equal collection
timestamps are resolved by the later metric ID, so review IDs and totals are
deterministic without loading a job's full metric history into service memory.

## MCP tools

`ai-ops-mcp` 把 `/v1` 暴露成本地 stdio MCP tools。它通过 `AgentContractClient` 访问 HTTP API，
不直接使用 Python service 或数据库，因此 HTTP DTO、scope、幂等账本和错误语义仍是唯一契约真相。

MCP 只公布 7 个 Agent 工具：

| MCP tool | v1 operation |
|---|---|
| `stage_content` | `stage_content` |
| `plan_publication` | `plan_publication` |
| `request_approval` | `request_approval` |
| `schedule` | `schedule` |
| `get_job_status` | `get_job_status` |
| `collect_metrics` | `collect_metrics` |
| `review_performance` | `review_performance` |

Mutating tools use `{"request": <v1 request DTO>, "idempotency_key": "..."}` and require the
same explicit 8–128 character idempotency key as HTTP. `get_job_status` uses `{"job_id": <int>}`;
`review_performance` uses `{"request": <PerformanceReviewRequest>}`. The bridge does not blindly
retry writes or generate idempotency keys; durable replay and conflicts are decided by the existing
operation ledger. Success is the corresponding v1 response DTO. Failure is marked as an MCP tool
error while retaining the stable structured envelope
`{"schema_version":1,"error":{"code":"...","message":"..."}}`.
`get_approval`, `download_approval_asset`, and `decide_approval` are deliberately absent because
they require a separate human principal. A human continues to review and decide through the
HTTP/CLI environment described below; the Agent must not receive that token.

The bridge reads `AI_OPS_URL` and the Agent Bearer `AI_OPS_TOKEN` from its process environment.
The token is never a tool argument and the bridge does not use legacy `API_KEY`. stdout is reserved
for MCP protocol messages. See [MCP Agent bridge](mcp.md) for Codex setup, side-effect boundaries,
and operational limitations.

## Python and CLI clients

`AgentContractClient` exposes methods with the same operation names and returns
the strict response DTOs. The script-oriented CLI is a thin wrapper around that
HTTP client:

For example, `stage.json` can contain:

```json
{
  "schema_version": 1,
  "topic_id": 4,
  "title": "Reviewed article",
  "body": "# Exact approved body",
  "content_type": "long_article",
  "target_platforms": ["zhihu"],
  "extra": {"zhihu_topic_ids": ["123456"]},
  "assets": []
}
```

After staging returns `content_id`, `plan.json` must select the intended
accounts explicitly:

```json
{
  "schema_version": 1,
  "content_id": 17,
  "account_ids": [3],
  "planned_for": "2026-08-12T02:00:00Z"
}
```

```bash
export AI_OPS_URL=http://127.0.0.1:8000
export AI_OPS_TOKEN='<from your secret store>'

ai-ops agent stage-content --input stage.json --idempotency-key stage-20260811-001
ai-ops agent plan-publication --input plan.json
ai-ops agent request-approval --input approval.json
ai-ops agent schedule --input schedule.json
ai-ops agent get-job-status 42
ai-ops agent collect-metrics --input metrics.json
ai-ops agent review-performance --input review.json
```

The human approval environment uses its separate token to review first and
then decide:

```bash
ai-ops agent get-approval <approval-id>
ai-ops agent download-approval-asset <approval-id> <asset-id> \
  --output ./reviewed/asset.png
ai-ops agent decide-approval <approval-id> --input decision.json
```

The output directory must already exist and the asset output must be new. On
POSIX it must be owned by the current user and must not be group/world
writable. The client refuses overwrite, streams with a 512 MiB default
ceiling, verifies the declared length and SHA-256, fsyncs, and atomically
commits the file with mode
`0600` where supported. Corrupt or partial downloads are removed. The CLI saves
bytes only to `--output` and writes one JSON metadata document to stdout.

Copy the reviewed bundle's `plan_digest` into `decision.json` as
`expected_plan_digest`. Input may be `-` for stdin. Other CLI stdout is also
exactly one JSON document; the bearer token has no CLI option and is never
placed in process arguments.

## Compatibility and current limits

- Existing non-`/v1` routes remain legacy administrator routes protected by
  a non-empty, strong, separately generated `X-API-Key`. They include explicit
  side-effect operations and are intentionally not Agent capabilities: do not
  give that key to an Agent, reuse it as a bearer token, or expose the legacy
  surface without TLS and an operator access boundary.
- The Python service enforces the same scopes and approval rules as HTTP. The
  CLI and stdio MCP bridge are HTTP clients and do not open the database directly.
- `approval:read` grants access to both the redacted review bundle and its exact
  asset bytes. Treat it as sensitive human-review access, not harmless metadata
  read permission.
- v1 exact planning currently supports only the enabled Zhihu CLI `0.2.4`
  renderer with an explicitly configured stable external account identity.
  YouTube CLI remains available only to legacy canary jobs until its OAuth
  profile can be bound to a channel through an audited read-only probe. Other
  platform paths fail closed and exact jobs never use Publisher fallback. This
  narrow execution guarantee does not promote either platform to Stable.
- `collect_metrics` is a scoped manual collection with a durable idempotency
  lease and snapshot binding. Automatic collection is a separate durable ledger:
  successful publications create fixed 1h/24h/7d tasks, the worker recovers them
  through fenced expiring leases and bounded retries, and each task uniquely binds
  one scheduled snapshot. The 1h/24h/7d deadlines allow at most 1h/6h/24h of
  lateness; expired windows fail instead of relabeling a current observation.
  These reads continue for already-published jobs when `AUTO_PUBLISH_ENABLED=false`.
- A v1 approval proves which configured principal reviewed and decided an
  exact digest, with a server-side snapshot recheck at decision time. It does
  not by itself prove platform deployment or readback. Platform maturity
  remains governed by the evidence matrix and dedicated-account canaries.
