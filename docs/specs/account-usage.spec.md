# Account usage

# 1. Purpose and Scope

Show, per provider account, what it spent and how much quota it has left:

- **Tokens:** how many tokens this account spent, over a chosen time window
  (Today, 24h, 7 days, 30 days, All time). Shows input, output, cached input,
  runs, and cost when one is reported.
- **Quota left:** what the provider says is left. For Codex: % used in the
  current 5h window and the weekly window, with reset times. For Cursor: per
  pool, whether the limit was hit and when it resets.

These are two different kinds of fact, and they are labelled apart. Tokens are
**counted by Argus** from what workers report. Quota is **the provider's
figure**, and it is shown with its source and age. A missing figure reads
"unknown" or "not reported", never 0 or 100%.

The Accounts tab shows all of it: provider marks, state, quota per window,
a token sparkline, and a detail panel with charts. Readable by the manager
scope (orchestrator, admin), as `GET /api/accounts` is. There is no MCP tool yet.

# 2. Non-Goals

- No price table here: this view shows cost only as reported, never estimated
  from tokens (estimates live in the [cost view](cost-view.spec.md), labelled).
  For subscription accounts it is hidden, since the quota is what matters there.
- No near-limit alerts or thresholds (home spec). A window at 100% is already
  reported as `exhausted`.
- No polling of idle accounts, and no polling of a provider that has no
  read-only, documented quota call. Claude stays opt-in (§4).
- No change to a credential as a side effect of reading quota.
- No per-pool routing yet: exhausting one Cursor pool still marks the whole
  account exhausted (§4).
- No history of quota snapshots: only the latest one per window is kept.

# 3. Decisions Made

## 3.1 Storage

- `account_usage` keeps one row per report, kept forever and aggregated on read.
  It gains `cached_input_tokens`, `model`, `engine` and `role`
  (`engine | executor`).
- `account_quota` keeps the latest snapshot per (account, window). The fields:
  - `window`: `"<minutes>m"` for timed windows, or a pool name such as
    `cursor` or `third_party`;
  - `window_minutes`;
  - `used_percent`: null for two-state pools;
  - `resets_at`, `exhausted_until`, `observed_at`;
  - `source`: `reported`, `polled` or `estimated`.
- A report for the same window replaces the snapshot. The UI labels 300 minutes
  "5h", 10080 "Weekly", and anything else by its duration.
- Accounts gain an optional `billing_day` (1–31). Cursor resets its pools on
  the billing date, and Argus uses it only to estimate a reset.

## 3.2 Ingest

- Workers report one `POST /api/accounts/{id}/usage` per engine run, for each
  account held (engine and executor separately), whatever the outcome.
- The caller must hold an active lease on the account (`403` otherwise).
- The body carries:
  - `input_tokens`, `output_tokens`, `cached_input_tokens`;
  - `model`, `engine`, `role`, `cost_usd`, `task_id`;
  - optional `run_key` (at most 200 characters; the client builds it from
    task, agent, engine and start): a repeated report of the same key for the
    account returns `204` and adds no row. Without a key every report is a row;
  - `measurement`: `measured` (default) or `not_reported`. A `not_reported` run
    happened but the engine gave no figures, so every token and cost field must
    be null. A `measured` report needs at least one of the token or cost
    fields. Both rules are validation errors: a missing figure is never
    stored as 0;
  - optional `rate_limits: [{window_minutes | pool, used_percent, resets_at,
    exhausted_until}]`.
- `result.usage` on task completion maps to the same insert.
- Usage rows emit no event (the run's task events already refresh views). A
  quota snapshot that changes emits `account.quota`.

Account credentials are returned only by a lease request with a required,
non-empty `runtime_providers` list and a `task_id`/`attempt` currently claimed
by that agent. Exhaustion reports require the same live task claim and return
fallback account metadata only; the worker obtains any next credential through
the task-bound lease endpoint. Agent credential rotation also requires an active
account lease and must preserve the document structure and JSON value types.

## 3.3 Polling (Codex)

- When a run ends (a task leaves `claimed`) without a quota snapshot observed
  since that run's claim, or when an account reports exhaustion, Argus queues a
  probe of the accounts the agent held. It also queues one on "Refresh quota".
  Probes are coalesced per account, and an account is probed at most once
  per 5 minutes.
- The probe runs `ARGUS_CODEX_BIN app-server` (the binary the Codex login
  already uses) in a throwaway `CODEX_HOME` holding only that account's
  `auth.json`. It calls the read-only `account/rateLimits/read` and stores
  `primary` and `secondary` as `polled` snapshots. It has a 15 s timeout, and
  the directory is deleted afterwards.
- **Credentials are never refreshed by the probe.** A ChatGPT login with less
  than 10 minutes of access token left is skipped ("the holder will refresh
  it"). The temporary copy is marked freshly refreshed, so the client does not
  rotate it proactively.
- If a rotation happens anyway, the rotated document is written back to the
  secret store so the stored credential stays valid, and
  `account.credential_rotated` is recorded.
- API-key accounts, other providers, and a missing binary are skipped. Their
  quota stays unknown.
- The probe runs outside any write transaction. Its result is written in a
  short one afterwards.

## 3.4 Cursor

- Cursor exposes no percentages, so each of its two monthly pools (`cursor`
  for its own models, `third_party` for API-priced models) is two-state:
  "within limit" or "exhausted until <reset>".
- The worker names the pool from the model it ran, using the engine
  catalogue's mapping.
- The reset is the provider's, if the error gives one. Otherwise it comes
  from `billing_day` (`estimated`). Failing that, the account cooldown applies.
- With on-demand spending enabled, Cursor bills instead of erroring, so Argus
  never sees exhaustion. The account form says so.

## 3.5 Read API

- `GET /api/accounts` gains `quota: [...]`, plus `cached_input_tokens`,
  `runs` and `unmeasured_runs` in its usage totals. `runs` counts every row,
  `unmeasured_runs` the `not_reported` ones; tokens and cost sum measured rows
  only. The series totals and buckets carry the same two counts.
- Accounts gain `token_limits` (create and update API): an optional object of
  positive integers under `5h`, `24h`, `7d`, the most total (input + output)
  tokens the account may spend in that rolling window. Null clears it.
- The account read model carries `quota_status`:
  `{state: known|unknown, reason, windows: [{period, limit, used, remaining,
  used_percent, source: "estimated"}]}`. The first rule that applies wins:
  1. provider snapshots exist in `quota`: `known` (`windows` may be empty; the
     snapshots are in `quota`);
  2. `token_limits` is set: `known`, with `windows` computed from the local
     rolling sums of measured rows (`estimated`);
  3. the account has no usage rows: `unknown`, `no_usage_records`;
  4. an API-key account, or provider `anthropic` or `cursor`:
     `unknown`, `provider_does_not_expose`;
  5. otherwise `unknown`, `window_not_configured`.
  No figure is ever invented: `unknown` has no windows.
- `GET /api/accounts/usage?from&to&bucket=hour|day&tz=<IANA>[&account_id]`
  returns totals and a time series per account.
  - Buckets follow the viewer's timezone, so "Today" matches their clock.
  - A bucket with no rows is a measured zero.
  - An account that never reported has `reported: false`.
  - At most 1000 buckets.
- `POST /api/accounts/{id}/quota/refresh` (manager) runs the probe now. It
  returns the outcome (`updated | skipped | rate_limited | failed`, with
  `reason`) and the snapshots.

## 3.6 Dashboard (Accounts tab)

- **The list:**
  - inline SVG provider marks (`openai`, `anthropic`, `cursor`, `gitea`) and a
    letter badge for custom providers;
  - state icons;
  - a quota cell (a bar per timed window with its age, or a pill per pool);
  - a 7-day token sparkline.
- **The detail panel:**
  - the time-window filter (Today, 24h, 7 days, 30 days, All time, in the
    browser's timezone; hourly buckets up to 24h, daily beyond);
  - stacked input/output/cached bars;
  - totals and runs;
  - cost (API-key accounts only);
  - quota windows with source and age;
  - "Refresh quota".
- Charts are hand-written SVG with no new dependency.
- Usage is hidden for `gitea` accounts, which hold repository leases, not
  model quota.

## 3.7 Metrics

`/metrics` adds:

- `argus_account_tokens_total{provider,account,kind=input|output|cached}`
  (counter);
- `argus_account_quota_used_percent{provider,account,window}`, for windows
  with a percentage.

## 3.8 Worker reporting (`argus-delegate`)

- **What is parsed.** After every executor attempt, `engines/usage.mjs`
  `parseUsage(executor, stdout)` reads only what the CLI printed:
  - `claude`: the result object; tokens are summed over `modelUsage` (helper
    models included, matching `total_cost_usd`), else the top-level `usage`;
    `num_turns` is the turn count. Verified against a real result.
  - `codex`: `turn.completed` events, summed across turns. **Synthetic,
    unverified** (documented shape, no real sample).
  - `agent` (Cursor): the result object's `usage` (`inputTokens`,
    `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`). **Synthetic,
    unverified.**
  - `opencode`: `step_finish` events, `tokens {input, output, cache {read,
    write}}` and `cost`, summed. **Synthetic, unverified.**
- **One field rule for every executor.** `input_tokens` is the prompt tokens
  not served from cache (fresh input plus cache-creation tokens),
  `cached_input_tokens` is cache-read tokens, and the two never overlap
  (Codex's `input_tokens` includes its cached part, so the cached part is
  subtracted). `cost_usd` is the CLI's own figure or null; it is never
  computed.
- **No figures, no numbers.** Missing input or output tokens, unparseable
  output, or an unknown shape give `measurement: "not_reported"` with a
  reason code and null figures. An absent cache field counts as 0 only when
  input and output are present.
- **`run_key`** is `r1:<task_id>:<agent_id>:<engine>:<executor>:<started_at>`,
  each part reduced to `[A-Za-z0-9._-]` (others become `_`; a missing part is
  `none`), or `r1:<sha256>` when longer than 200 characters. `agent_id` is
  `ARGUS_AGENT_ID` or the hostname. It holds identifiers only. Every model
  attempt is its own run, with its own start time and key.
- **Spool file.** Always, `<out>/usage-<first 16 hex of sha256(run_key)>.json`
  holds `{account_id, payload}`, the exact body that is (or would be) sent, so
  a missed report can be replayed; the key makes a replay idempotent.
- **Best-effort POST.** Only when `ARGUS_URL` and `ARGUS_AGENT_TOKEN` are set
  and the pinned task context (`argus-model-policy.json`) carries
  `executor_account_id`. The body is `{task_id, model, engine, role:
  "executor", measurement, input_tokens, output_tokens, cached_input_tokens,
  cost_usd, run_key}`, where `engine` is the executor CLI that ran. The token
  is only in the `Authorization` header and never in the body, the spool file,
  the report or the logs. The call has a 5 s timeout; on any failure the
  delegate writes one line to stderr and goes on. It never throws and never
  changes its exit code.
- **Report fields.** The JSON report (and each entry of `attempts`) gains
  `usage`, `run_key`, `usage_spool` and `usage_report: {status:
  sent|skipped|failed, reason}`. `skipped` reasons: `no_account_id`,
  `no_api_env`; `failed` reasons: `http_<status>` or the error name.
- **Context keys.** The host pins `engine_account_id` and
  `executor_account_id` (the lease's `account_id`, or null). Without a
  separate executor lease the executor account is the engine's, as for
  `executor_account_provider`. The delegate always acts as role `executor`;
  `engine_account_id` is for a harness-side reporter.

# 4. Open Questions

- **Claude quota.** Only an undocumented OAuth usage endpoint exists. It stays
  off until that is accepted. Until then, Claude quota arrives only through
  worker reports (the NanoClaw harness patch).
- **Per-pool routing for Cursor:** keep the account leasable while one pool is
  exhausted and steer the worker to the other pool.
- **Codex `account/tokenUsage/read`** reports lifetime and daily tokens for the
  whole ChatGPT account, including use outside Argus. Whether to show it next
  to Argus's own count is undecided.

Model observability executions carry the usage `run_key`; the join exposes the
usage row of the same run, `null` when none ([spec](model-observability.spec.md)).

# 5. Constraints

- Single node, SQLite, no new service or dependency. The probe is an optional
  subprocess and needs `ARGUS_CODEX_BIN`.
- No outbound or subprocess call inside a write transaction. `tx.now` is the
  only clock in writes.
- Never log or return a credential. Probe output is parsed, never logged.

# 6. Implementation Consequences

- Migrations `0015_account_usage.sql` and `0019_account_usage_runs.sql`
  (`run_key` with a partial unique index, `measurement`, `token_limits`). Core models in `argus_core.accounts`.
- `accounts/service.py` (ingest, quota upsert, aggregation),
  `accounts/quota_probe.py` (the probe and its loop), and routes.
- Metrics in `ops.py`. `overview.quota_measured` becomes true once any account
  has a snapshot.
- Dashboard `views/Accounts.tsx`, plus `charts.tsx`, `providerIcons.tsx` and
  `usage.ts` (pure helpers, unit-tested).
- NanoClaw side: `engines/usage.mjs`, `argus-delegate.mjs` (§3.8) and the
  context keys pinned by `argus.ts`.
- Follow-ups:
  - NanoClaw executors report Cursor pool exhaustion;
  - a NanoClaw harness patch forwards the main engine's usage and rate limits.
