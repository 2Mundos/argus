# Fleet health

# 1. Purpose and Scope

Fleet is a dashboard tab (`#fleet`) that answers: are the pools alive, how long do
tasks wait and run, how often do they succeed, why do they fail, and how long
dead-session recovery takes after an observed `session_lost`. It is one
read-only endpoint, `GET /api/fleet/health?window=<hours>`, readable by the
orchestrator and admin scopes (never workers). `window` defaults to 24, must be
1..720 (30 days); anything else is a validation error (422).

It shows pools (status, `live`/`desired`, `running`, last heartbeat), a count per
`TaskState`, queue and run time (p50, p90, mean), success and failure rate,
failures grouped by reason with up to 5 recent examples each, yield, and
dead-session recovery coverage and latency.

# 2. Non-Goals

- No quota data and no per-account cost ([account usage](account-usage.spec.md) and
  [cost view](cost-view.spec.md) own them); only the per-task yield below.
- No write actions, no new tables, no time-series or history charts.
- No model availability verdict: a heartbeat proves a process is alive only.

# 3. Decisions Made

- **One snapshot.** Every query runs in one read transaction.
- **Unknown is not zero.** A missing value is `null` and the UI prints `no data`.
  Durations and rates are `null` when fewer than `MIN_SAMPLES` (5) samples exist;
  a mean or percentile of 1-4 samples is never reported. `n` is always present.
- **Provenance.** Every metric carries `window_hours` and `source`.
- **Cohort.** Duration and outcome metrics use tasks that reached `succeeded` or
  `failed` with `updated_at` inside the window (`cancelled` is neither).
  Queue time = `created_at` to the first `task.claim` event; run time = the last
  `task.claim` event to `updated_at` (for reviewed tasks this includes the review
  wait). A task with no claim event contributes to neither. Percentiles are linear
  interpolation, computed in Python.
- **Pools.** Every pool is listed, including one with no instances or agents (then
  `live`/`running` are 0 and `last_heartbeat_at` is `null`). `live` = instances not
  terminated. `last_heartbeat_at` = max `agents.last_heartbeat_at` of live
  (`active`/`draining`) agents on the pool's instances.
- **Tasks by state** is the current `tasks` snapshot, not windowed, with every state.
- **Failures** are `task.fail` and `task.expire` events in the window (retried
  attempts count; it is attempts that fail, not only final failures). Reason comes
  from `data.reason`; `classify_failure` in `fleet_health.py` is the only mapping:
  a system reason (`lease_expired`, `timeout`, `agent_ended`) or a worker's own
  code in the list is kept; a generic `worker_fail` is refined to `auth_missing`,
  `not_installed` or `gate_failed` when the error text contains that keyword;
  unknown reasons become `other`. Groups sort by count; excerpts are 200 chars.
- **Yield.** `yield` answers what the fleet got for what it spent. `attempts` and
  `completions` count `task.claim` and `task.complete` events in the window;
  `attempt_success_rate` is their ratio. Each failure group carries `lost_seconds`:
  the time its failed or expired attempts held a claim (last `task.claim` before
  the event), `null` when none has one. Tokens (input plus output) and cost come
  from the measured `account_usage` rows of the tasks in the finished cohort,
  split by outcome. Coverage counts attempts ([account usage](account-usage.spec.md)
  3.10): `measured_attempts` and `not_reported_attempts` over the cohort's
  attempts with a usage row; `usage_tasks` is tasks with any row and
  `measured_tasks` tasks whose every attempt is measured. Only measured tasks
  enter the token and cost means, so an unreported attempt is never counted as
  zero; a mean needs `MIN_SAMPLES` measured tasks (else `null`). Migration
  `0046` indexes `account_usage.task_id`. The MCP tool `get_fleet_yield(window_hours)`
  (managers) returns this `yield` block.
- **Dead-session recovery (P5 measure).** `dead_session_recovery` answers how long
  after a vanished session is *observed* until the task is claimed again. Cohort:
  `task.fail` events in the window with `data.reason = session_lost` (failure class
  `session` only; `stalled` / `lease_expired` / other reasons are out). There is
  **no death timestamp** in events: the channel fails after up to 90 s of a seen
  container being gone ([stall visibility](stall-visibility.spec.md) 3.1), and the
  fail's `at` is `observed_lost_at`. Fail and requeue are the same event when
  `data.to = ready` (latency fail→ready is always 0 by construction); recovery
  latency is therefore `observed_lost_at` → `at` of the paired claim.
  **Pairing is by event id**, the one monotonic order (an `at` can step back with
  the clock): a fail pairs with the first `task.claim` of the same task whose id
  is above the fail's and below the task's next `task.fail` / `task.expire` (if
  any), so a later attempt end never steals or shares a claim. Outcomes per
  synthetic sample:
  - `measured`: requeued and paired with a claim, latency ≥ 0.
  - `pending`: requeued, no paired claim, and the task is still waiting
    (`ready`, `pending` or `blocked`) in the same snapshot.
  - `ended_unclaimed`: requeued, no paired claim, and the task is now terminal
    (`cancelled`, `failed`, `succeeded`), e.g. cancelled while waiting. It never
    recovered, and it is not a latency.
  - `not_requeued`: `to` is not `ready` (attempts exhausted or unexpected `to`).
  - `anomalous`: unreadable payload or time, negative latency (the paired claim's
    `at` is before the fail's: clock rollback), an attempt end with no claim
    between, or no paired claim while the task row is missing or in any other
    state (`claimed`, `in_review`: a claim event must exist).

  `denominator` is the bounded fail count; `coverage` is `measured / denominator`
  (`null` when the denominator is 0). `p50` / `p90` / `mean` need `MIN_SAMPLES`
  measured latencies, else `null` (never a fabricated median). `outliers` counts
  measured samples with latency ≥ `target_s` (300 s, the draft P5 target);
  outliers stay in the distribution. **Censored samples:** `pending_overdue` counts
  pending samples whose age, request `now` − `observed_lost_at`, is ≥ `target_s`.
  The age is a lower bound of a latency not yet known, so it never enters
  percentiles, mean or outliers and is not a death time. A pending sample younger
  than the target may still recover in time. Examples (up to 5 newest measured)
  carry fail and claim event ids for audit. MCP
  `get_dead_session_recovery(window_hours)` (managers) returns this block. No
  scheduling, timeout, requeue or `ARGUS_STALL_FAIL_S` change. This block measures
  the P5 goal; it does not show that the 5-minute median is met.

  *Query cost and limits.* The cohort is read through `events_kind_at` (newest
  5000 fails in the window, `truncated` when hit) and materialized once. Each
  fail then does two `events_subject` range lookups (`subject_id = ?`,
  `id > fail id`, first match by id: next attempt end, then the claim below it),
  a primary-key lookup of the claim's `at` and a unique-key lookup of the task's
  state; the final order is one sort of at most 5000 rows. The cost grows with
  each task's own later events, not with unrelated history (measured below; asserted by
  `EXPLAIN QUERY PLAN` in the tests: no `SCAN` of `events` under any alias, `SCAN e`
  included, and each lookup uses its expected index, so a kind-only lookup fails).
  Window membership still uses the fail's `at`, so a fail whose `at` was written
  during a clock rollback may fall outside the window (pinned by a test).
  `pending` and `ended_unclaimed` read the task's *current* state; the window does
  not freeze it. `pending` covers `ready`, `pending` and `blocked`; `pending_overdue`
  is the age of those samples since the observed loss, i.e. time unclaimed,
  not worker speed. A negative age (fail stamped after `now`) is never overdue.

  *Measured (evidence, not a bound).* One local run: isolated SQLite 3.40.1 in a
  sandbox, disposable DB built from the real migrations, median of 3 runs of
  `SESSION_LOST_SQL` with `MAX_ROWS` 5000. The harness is not committed and no test
  asserts timing, so these figures are not reproducible from the repo. Hardware and
  production numbers differ.
  - 1000 fails, 3k events total: 2.7 ms. 1000 fails with 200k unrelated events: 2.7 ms,
    so in that run unrelated history did not slow the query.
  - Same 1000 fails, with 100 / 1000 / 5000 `task.progress` events of each task
    between its fail and claim: 21 / 171 / 802 ms. The per-task walk, not the table,
    is the cost; in that run it grew roughly linearly with those later events.
  - 5000 fails (3600 in window), 215k events: about 10 ms.
  - Regression contrast, 1000 fails with 200k unrelated events: dropping
    `events_kind_at` gives `SCAN e` at 9.0 ms; dropping `events_subject` gives
    kind-only correlated lookups at 376 ms.
  - Hypotheses, not measured: the production event volume per task, and cost on the
    production disk. The query is not constant time; the bounds are the 5000-fail cap
    and each task's later events before its next end or claim.
- **Bounded reads.** No full scan of `tasks` or `events`: migration
  `0021_fleet_health_indexes.sql` adds `tasks_finished` and `events_kind_at`; per-task
  claim lookups use `events_subject`. Each fetch keeps at most 5000 newest rows and
  sets `truncated` when it hits that.

# 4. Open Questions

- Should reasons `channel_error` and `provider_failure` be emitted by workers
  explicitly? Today they only appear if a worker sends them as the fail reason.
- Should `stalled` (and other session-class expires) join this recovery report as
  a separate series, or stay out so the P5 dead-session path stays pure?

# 5. Constraints

- Single node, SQLite only; reads never emit events or take the write lock.
- UI text is English; layout works from 390 px.

# 6. Implementation Consequences

- Backend: `apps/api/src/argus_api/fleet_health.py`, route in `ops_routes.py`,
  MCP `get_dead_session_recovery`; tests in `apps/api/tests/test_fleet_health.py`
  and `apps/api/tests/test_dead_session_recovery.py`.
- Frontend: `views/Fleet.tsx`, Fleet tab (`#fleet`) in `App.tsx`; `#tasks/<id>` opens that
  task's detail in the Tasks tab.
- Regenerate with `make openapi`.
