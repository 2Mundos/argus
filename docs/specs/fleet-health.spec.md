# Fleet health

# 1. Purpose and Scope

Fleet is a dashboard tab (`#fleet`) that answers: are the pools alive, how long do
tasks wait and run, how often do they succeed, and why do they fail. It is one
read-only endpoint, `GET /api/fleet/health?window=<hours>`, readable by the
orchestrator and admin scopes (never workers). `window` defaults to 24, must be
1..720 (30 days); anything else is a validation error (422).

It shows pools (status, `live`/`desired`, `running`, last heartbeat), a count per
`TaskState`, queue and run time (p50, p90, mean), success and failure rate, and
failures grouped by reason with up to 5 recent examples each.

# 2. Non-Goals

- No cost, token or quota data ([account usage](account-usage.spec.md) owns it).
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
- **Bounded reads.** No full scan of `tasks` or `events`: migration
  `0020_fleet_health_indexes.sql` adds `tasks_finished` and `events_kind_at`; per-task
  claim lookups use `events_subject`. Each fetch keeps at most 5000 newest rows and
  sets `truncated` when it hits that.

# 4. Open Questions

- Should reasons `channel_error` and `provider_failure` be emitted by workers
  explicitly? Today they only appear if a worker sends them as the fail reason.

# 5. Constraints

- Single node, SQLite only; reads never emit events or take the write lock.
- UI text is English; layout works from 390 px.

# 6. Implementation Consequences

- Backend: `apps/api/src/argus_api/fleet_health.py`, route in `ops_routes.py`,
  tests in `apps/api/tests/test_fleet_health.py`.
- Frontend: `views/Fleet.tsx`, Fleet tab (`#fleet`) in `App.tsx`; `#tasks/<id>` opens that
  task's detail in the Tasks tab.
- Regenerate with `make openapi`.
