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
  latency is therefore `observed_lost_at` → the next `task.claim` for that task
  with `at` after the fail (or the same `at` and a higher event id). Outcomes per
  synthetic sample: `measured` (requeued and a later claim), `pending` (requeued,
  no later claim yet), `not_requeued` (`to` is not `ready`), `anomalous`
  (unreadable payload or negative latency). `denominator` is the bounded fail
  count; `coverage` is `measured / denominator` (`null` when the denominator is
  0). `p50` / `p90` / `mean` need `MIN_SAMPLES` measured latencies, else `null`
  (never a fabricated median). `outliers` counts measured samples with latency
  ≥ `target_s` (300 s, the draft P5 target); outliers stay in the distribution.
  Examples (up to 5 newest measured) carry fail and claim event ids for audit.
  MCP `get_dead_session_recovery(window_hours)` (managers) returns this block.
  No scheduling, timeout, or `ARGUS_STALL_FAIL_S` change.
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
