# Account usage

# 1. Purpose and Scope

Show, per provider account, what it spent and how much quota it has left:

- **Tokens:** how many tokens this account spent, over a chosen time window
  (Today, 24h, 7 days, 30 days, All time). Shows input, output, cached input,
  runs, and cost when one is reported.
- **Quota left:** what the provider says is left, for every account that has a
  read-only source:
  - Codex: % used in the 5h and weekly windows, with reset times.
  - Claude (a full login): % used in the session (5h), weekly and per-model
    weekly windows, with reset times.
  - Cursor (an imported login): credit grants and the billing-cycle reset, read
    from the account's own dashboard calls (§3.3); and, from worker reports, per
    pool whether the limit was hit and when it resets (§3.4).
- **Credit left:** money, not a window. An OpenRouter key's spending limit and
  what remains of it, and a Cursor login's credit grants, in USD.

These are different kinds of fact, and they are labelled apart: **measured
provider quota** (a window), **remaining credit** (USD on a key), **local
estimate** (`token_limits`), and **unknown** (with the reason: unsupported, needs
a permission or credential Argus lacks, or the read failed). One is never shown
as another. Tokens are
**counted by Argus** from what workers report. Quota is **the provider's
figure**, and it is shown with its source and age. A missing figure reads
"unknown" or "not reported", never 0 or 100%.

The Accounts tab shows all of it: provider marks, state, quota per window,
a token sparkline, and a detail panel with charts. Readable by the manager
scope (orchestrator, admin), as `GET /api/accounts` is. The MCP tools `list_accounts` and `refresh_account_quota` serve the same data (§3.5).

# 2. Non-Goals

- No price table here: this view shows cost only as reported, never estimated
  from tokens (estimates live in the [cost view](cost-view.spec.md), labelled).
  For subscription accounts it is hidden, since the quota is what matters there.
- No near-limit alerts or thresholds (home spec). A window at 100% is already
  reported as `exhausted`.
- No polling of idle accounts, and no polling of an account kind with no
  read-only quota call (an API key that only inference accepts, Cursor, the
  local model). They are named as such (§3.5), not polled.
- No browser sessions, cookies or scraped pages as a quota source, and no
  silent refresh of a login to read it.
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
  - A Claude window the provider marks locked (`locked_reason`) has no figure:
    it is not stored and never read as 0% used (§3.3).
  - Per-model weekly Claude windows are pools (`weekly_opus`, `weekly_sonnet`,
    `weekly_<model>`); a window the provider returns as null is absent, never 0.
- `account_credit` keeps the latest credit reading per account: `unit` (`usd`),
  `limit_amount` (null: the key has no cap), `remaining_amount` (null only for
  an uncapped key), `used_amount`, `limit_reset`, `observed_at`, `source`
  (`polled`). Credit is the key's, not the provider account's balance, and is
  never mixed with token windows or Argus's own reported cost.
- `account_quota_probe` also keeps the machine-readable `code` of the outcome.
  A probe that does not update changes only this row: the last good windows and
  credit stay, and age into `stale`.
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
  - optional `reason` (`[a-z0-9_]{1,60}`): only with `not_reported`, stored as
    `unmeasured_reason` on the row and aggregated in read APIs as
    `unmeasured_reasons` counts (missing reason reads as `unknown` in totals);
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

## 3.3 Polling

- **What can be read.** The source depends on the account, not on a hardcoded
  provider verdict:
  - **Codex ChatGPT login:** the official client (below).
  - **Claude login** (`claudeAiOauth`, or an `oauth_token`): `GET
    https://api.anthropic.com/api/oauth/usage` with the account's own access
    token and `anthropic-beta: oauth-2025-04-20`. This is the endpoint Claude
    Code's own usage view calls. It is **not in Anthropic's public API
    documentation**, so it may change; a response that does not parse is
    `bad_response`, never zeros. `utilization` (and `percent` in the structured
    `limits` list) is the share **used**, a number or a numeric string. Windows
    with a `locked_reason` are locked, not unused: they get no figure and are
    named in the probe note (`window_locked` when every window is locked).
    `limits` entries with `is_active: false` are skipped, and ones that repeat a
    flat window are not read twice. Dollar fields, `severity`, `extra_usage` and
    `spend` are not read. The token needs the profile scope that
    a full login has; a `setup-token` credential is inference-only, so the call
    is refused and the account reads `oauth_scope_missing`.
  - **OpenRouter key** (custom account with label `openrouter`): `GET
    {base_url}/key` with the key as bearer; `limit`, `limit_remaining`, `usage`
    and `limit_reset` become the credit (USD). `limit: null` is an uncapped key
    (no remainder is shown); a capped key without `limit_remaining` is refused.
  - **Cursor login** (`accessToken`, kind `subscription`): three POSTs of `{}`
    to `https://api2.cursor.sh/aiserver.v1.DashboardService/` as Connect JSON
    (`Authorization: Bearer <accessToken>`, `Connect-Protocol-Version: 1`):
    `GetCreditGrantsBalance`, `GetUsageLimitStatusAndActiveGrants` and
    `GetPlanInfo`. One client, 15 s in all, no redirects, bounded body. These
    are the dashboard's own calls and **not a documented public API**, so a
    changed shape reads as `bad_response`. int64 fields (cents, `*Ms`) arrive as
    **strings**. Credit is read only from explicit numeric fields:
    `totalCents`/`usedCents`/`creditBalanceCents`, or `activeGrants[]`
    `remainingCents` and `totalCents` summed (only when every grant states
    both). A missing field is absent, never 0 (`{}` is a normal answer). The
    reset is `resetAtMs` or a dated `planInfo.billingCycleEnd`. If the login
    answers with no numeric usage or credit (limits and plan only), the outcome
    is `skipped` / `no_usage_window_for_login`, naming the plan period and cycle
    end when stated; no percentage or 0 is shown.
  - **No source:** Anthropic and OpenAI API keys (inference-only; usage windows
    belong to a login), a **Cursor API key** (its usage is the team Admin API's,
    read with an admin key, a different source that the login path does not
    replace: `admin_key_required`), the local model (no provider quota), other custom providers (use
    `token_limits` estimates). These are named, not polled.
- **Triggers.** A pollable account is probed when: a run ends (a task leaves
  `claimed`) without a snapshot observed since that run's claim, an account
  reports exhaustion, it is created or its credential is renewed, "Refresh
  quota" is used, and by the sweep. Probes are coalesced per account and an
  account is probed at most once per 5 minutes. No trigger depends on a task
  ending: the sweep alone keeps every pollable account fresh.
- **Sweep.** Accounts that are pollable and whose latest snapshot, credit or
  probe is older than 30 minutes: the first sweep runs 10 s after startup, then
  at most every 15 minutes, up to 10 accounts per sweep with a 2 s gap between
  probes that ran. The latest probe outcome per account (`updated | skipped |
  rate_limited | failed`, with a `code` and a short `reason`) is stored apart
  from the figures.
- **Outcome codes** (a probe that did not update says which): `probe_disabled`
  (reader not installed or configured), `credential_missing`,
  `credential_invalid`, `credential_expired`, `credential_rejected` (HTTP 401),
  `oauth_scope_missing` (HTTP 403 on the usage call), `api_key_account`,
  `admin_key_required`, `no_provider_source`, `local_no_quota`,
  `provider_rate_limited` (HTTP 429), `fetch_failed` (network, 5xx),
  `bad_response`, `no_windows`, `no_usage_window_for_login`, `window_locked`. A probe that never ran reads `not_yet_probed`.
  `skipped` means nothing was asked; `failed` means it was asked and did not
  answer well. Neither erases figures nor fails a worker task.
- **Codex.** The probe runs `ARGUS_CODEX_BIN app-server` in a throwaway
  `CODEX_HOME` holding only that account's `auth.json`, calls the read-only
  `account/rateLimits/read`, and stores `primary` and `secondary` as `polled`
  snapshots. It has a 15 s timeout, and the directory is deleted afterwards.
- **Credentials are never refreshed by a probe.** A ChatGPT login with less
  than 10 minutes of access token left, and an expired Claude access token, are
  skipped ("the holder will refresh it"). The Codex temporary copy is marked
  freshly refreshed so the client does not rotate it proactively. If a rotation
  happens anyway, the rotated document is written back and
  `account.credential_rotated` is recorded.
- **Requests.** Each Claude and OpenRouter read is one GET (Cursor: three POSTs, 15 s in all) with a 15 s timeout,
  to the provider's own host, no redirects. The credential goes only in the
  `Authorization` header of that request and never into a log, event, error or
  response; error text is ours, never the provider's body.
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
  4. provider snapshots or credit exist but are stale: `stale` (shown with their
     age; never dropped);
  5. no readable source for the account: `unknown` with the reason for it
     (`api_key_account` for an OpenAI/Anthropic key, `admin_key_required` for
     a Cursor API key, `local_no_quota`, else `no_provider_source`);
  6. a readable source, last probe not updated: `unknown` with the probe's
     `code` (§3.3), or `probe_failed | probe_skipped` without one;
  7. otherwise `unknown`, `not_yet_probed`.
  The status also carries `credit`, `observed_at`, `collector`
  (`codex_rate_limits | claude_oauth_usage | cursor_dashboard | openrouter_key |
  worker_report`) and
  `last_probe`. Credit makes the status `known` while fresh. No figure is ever
  invented: `unknown` has no windows, and unknown is never zero.
- `GET /api/accounts/usage?from&to&bucket=hour|day&tz=<IANA>[&account_id]`
  returns totals and a time series per account.
  - Buckets follow the viewer's timezone, so "Today" matches their clock.
  - A bucket with no rows is a measured zero.
  - An account that never reported has `reported: false`.
  - At most 1000 buckets.
- `POST /api/accounts/{id}/quota/refresh` (manager) runs the probe now. It
  returns the outcome (`updated | skipped | rate_limited | failed`, with
  `code` and `reason`), the snapshots, the credit, and the account's resulting
  `quota_status`. A `rate_limited` answer carries the last stored figures, not
  a success. Accounts also carry `credit` (null when none read).
- **MCP** (managers): `list_accounts` returns the same account read model, and
  `refresh_account_quota(account_id)` the same refresh answer as the endpoint.

## 3.6 Dashboard (Accounts tab)

- **The list:**
  - inline SVG provider marks (`openai`, `anthropic`, `cursor`, `gitea`) and a
    letter badge for custom providers;
  - state icons;
  - a quota cell (a bar per timed window with its age, a pill per pool, a
    credit line in USD, or the reason nothing is known, never a bare dash);
  - a 7-day token sparkline.
- **The detail panel:**
  - the time-window filter (Today, 24h, 7 days, 30 days, All time, in the
    browser's timezone; hourly buckets up to 24h, daily beyond);
  - stacked input/output/cached bars;
  - totals and runs;
  - cost (API-key accounts only);
  - quota windows and credit with source and age, and the last probe's outcome;
  - "Refresh quota" on every account with model usage. The answer is worded by
    outcome and code: a skip or failure is never shown as an update, and a
    failure says the last good figures are kept.
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
- **Best-effort POST or host relay.** When `ARGUS_URL` and `ARGUS_AGENT_TOKEN`
  are set in the delegate process and the pinned task context
  (`argus-model-policy.json`) carries `executor_account_id`, the delegate POSTs
  directly. Otherwise it appends the same body (plus `task_id`, `attempt`,
  `account_id`, `role: "executor"`) as one JSON line to `argus-usage.jsonl` in
  the agent group's mounted directory for the NanoClaw host to post (§3.9). The
  body is `{task_id, model, engine, role, measurement, input_tokens,
  output_tokens, cached_input_tokens, cost_usd, run_key, reason?}`, where
  `engine` is the executor CLI that ran. Credentials stay only in the
  `Authorization` header on a direct POST and never appear in the relay file,
  spool file, report or logs. A direct POST has a 5 s timeout; on failure the
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

## 3.9 NanoClaw host relay (engine and executor)

Agent containers hold no Argus token. Both roles append **untrusted** usage
records to `argus-usage.jsonl` (same path in the group mount; 4 MiB cap per
file, further records dropped):

- **Executor** (`engines/usage.mjs` via `argus-delegate.mjs`): one record per
  delegated CLI attempt, `source: "delegate"`, `role: "executor"`, figures from
  `parseUsage`, `run_key` as in §3.8. When a direct POST is skipped
  (`usage_report.reason=no_api_env`), the relay line is still written when
  possible.
- **Engine** (`overlays/harness-usage.ts`, patched into the agent runner's
  `claude.ts` and `codex.ts` when present): one record per harness turn that
  spent tokens or explicitly failed to measure, `source: "harness"`,
  `role: "engine"`, `run_key` `h1:<task>:<attempt>:<engine>:<query>:<seq>`.

Each line is `{v:1, kind:"usage", source, role, task_id, attempt, account_id,
payload}` where `account_id` in the file is the reporter's view; the host
**never** trusts it for billing.

On the host, `argus-usage-relay.ts` (installed into the NanoClaw channel) tails
the file for the claimed task:

- Validates envelope, `task_id`, `attempt`, `role` and rebuilds the POST body
  from `payload`.
- Bills the account the host actually holds for that role: `engine` →
  `engine_account_id`; `executor` → `executor_account_id`, or the engine's
  account when no separate executor lease exists (same lease, never doubled).
- Drops records for another task, a mismatched account, bad figures, symlinks on
  the relay path, or HTTP 4xx other than retryable codes; retries transient
  failures with backoff and drains pending lines for up to 10 s after the task
  ends.
- Posts `POST /api/accounts/{id}/usage` with the host's agent token.

- At the end of an attempt for which it posted no usage at all, it posts one
  `not_reported` row for the engine account with `reason: "no_usage_seen"` and
  `run_key` `h0:<task>:<attempt>`, so every attempt has at least one row
  ([worker efficiency](../drafts/2026-10-24.worker-efficiency.draft.md) P8).

Idempotency is unchanged: `run_key` on the API deduplicates executor runs;
harness keys deduplicate per turn. Measured stream totals use deltas so resume
and cache accounting do not double-count.

# 4. Open Questions

- **Claude usage endpoint stability.** It is read as Claude Code's own client
  reads it but is undocumented. If Anthropic documents or changes it, §3.3
  follows; until then a changed shape reads as `bad_response`.
- **Cursor usage with an API key.** Only the team Admin API (an admin key,
  Enterprise) reports it. That needs an admin key registered as its own
  credential kind; not implemented.
- **Cursor usage shape.** The dashboard calls are read as the Cursor client
  makes them. Which numeric fields a given plan fills (grants, balance) is
  confirmed per plan; a login with none reads `no_usage_window_for_login`.
- **Per-pool routing for Cursor:** keep the account leasable while one pool is
  exhausted and steer the worker to the other pool.
- **Codex `account/tokenUsage/read`** reports lifetime and daily tokens for the
  whole ChatGPT account, including use outside Argus. Whether to show it next
  to Argus's own count is undecided.

Model observability executions carry the usage `run_key`; the join exposes the
usage row of the same run, `null` when none ([spec](model-observability.spec.md)).

# 5. Constraints

- Single node, SQLite, no new service or dependency. The Codex probe is an
  optional subprocess and needs `ARGUS_CODEX_BIN`; the Claude and OpenRouter
  reads are plain HTTPS GETs, and the Cursor read three POSTs; none needs
  anything installed.
- No outbound or subprocess call inside a write transaction. `tx.now` is the
  only clock in writes.
- Never log or return a credential. Probe output is parsed, never logged.

# 6. Implementation Consequences

- Migrations `0015_account_usage.sql`, `0019_account_usage_runs.sql`
  (`run_key` with a partial unique index, `measurement`, `token_limits`) and
  `0039_usage_telemetry.sql` (`unmeasured_reason`, `account_quota_probe`) and
  `0042_quota_sources.sql` (probe `code`, `account_credit`). Core
  models in `argus_core.accounts`.
- `accounts/service.py` (ingest, quota upsert, aggregation),
  `accounts/quota_probe.py` (the probe and its loop), `accounts/quota_readers.py`
  (the Claude, Cursor and OpenRouter requests and parsers), routes, and the MCP tools
  `list_accounts` and `refresh_account_quota`.
- Metrics in `ops.py`. `overview.quota_measured` becomes true once any account
  has a snapshot.
- Dashboard `views/Accounts.tsx`, plus `charts.tsx`, `providerIcons.tsx` and
  `usage.ts` (pure helpers, unit-tested).
- NanoClaw side: `engines/usage.mjs`, `argus-delegate.mjs` (§3.8),
  `argus-usage-relay.ts`, `overlays/harness-usage.ts`, `patch-harness-usage.py`,
  wired by `install.sh` and the context keys pinned by `argus.ts` (§3.9).
- Follow-ups:
  - NanoClaw executors report Cursor pool exhaustion.
