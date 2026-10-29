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

  *Query cost and limits.* The cohort is read through `events_kind_at`
  (`kind = 'task.fail'` and an `at` range). That scan visits every `task.fail`
  newer than the cohort's oldest fail (the 5000th newest `session_lost`),
  including other reasons; when the window has fewer than 5000 `session_lost`
  fails it is every `task.fail` in the window. It filters by `json_extract(data,
  '$.reason') = 'session_lost'`; the `lost` CTE then applies `ORDER BY at DESC,
  id DESC LIMIT ?` after that predicate, so the cohort is the newest 5000
  `session_lost` fails, materialized once. Each fail
  then does two `events_subject` range lookups (`subject_id = ?`, `id > fail id`,
  first match by id): `f`, the next `task.fail`/`task.expire`, and `c`, the claim
  below that end. Only `c` is bounded by the next end (`c.id < next_end_id`). `f`
  has no upper bound from the claim, so it visits the task's own later events,
  including post-claim `task.progress`, until its next end or its last event.
  The rest is a primary-key lookup of the claim's `at`, a unique-key lookup of the
  task's state, and one sort of at most 5000 rows. The cost grows with each task's
  own later events and with other-reason `task.fail` events in the window, not
  with unrelated non-fail history. The tests assert plan shape via
  `EXPLAIN QUERY PLAN`: no `SCAN` of `events` under any alias, `SCAN e` included,
  and each lookup uses its expected index, so a kind-only lookup fails. They assert
  no timing. Bounding `f` by the next claim is a possible follow-up, not
  implemented. Window membership still uses the fail's `at`, so a fail whose `at`
  was written during a clock rollback may fall outside the window (pinned by a
  test). `pending` and `ended_unclaimed` read the task's *current* state; the
  window does not freeze it. `pending` covers `ready`, `pending` and `blocked`, so
  it counts tasks waiting for a worker together with tasks blocked for another
  reason; `pending_overdue` is the age of those samples since the observed loss,
  i.e. time unclaimed, not worker speed. A negative age (fail stamped after `now`)
  is never overdue.

  The 5000 cap (`MAX_ROWS`) limits how many `session_lost` fails are processed:
  when the window holds more, the newest 5000 are processed, older ones are not,
  and `truncated` is set. It also bounds the `task.fail` scan, which stops at the
  5000th match; other-reason rows newer than that position are still visited and
  filtered. It does not bound the work per task history, which is the `f` walk
  above.

  *Measured (evidence, not a bound).* `apps/api/bench/dead_session_recovery_bench.py`
  builds a disposable in-memory SQLite DB from the real migrations, seeds 1000
  `session_lost` fails each followed by a re-claim and N `task.progress` events,
  and times `SESSION_LOST_SQL` (median of 3 runs, `MAX_ROWS` 5000, 1000 rows
  returned). One run of the default sweep (median of 3 per point) on an Intel
  Core i9-12900KF (12th Gen), 24 cores, 62.6 GB RAM, Linux 6.17.0-41-generic,
  Python 3.13.17, SQLite 3.53.1:

  | post-claim events per task | median ms |
  | --- | --- |
  | 0 | 2.1 |
  | 100 | 8.4 |
  | 1000 | 59.7 |
  | 5000 | 270.4 |

  Command: `uv run python apps/api/bench/dead_session_recovery_bench.py --out /tmp/default.json`

  The plan is `SEARCH f USING INDEX events_subject (subject_type=? AND subject_id=?
  AND id>?)` with no `id` upper bound, so cost grows roughly linearly with a task's
  later events. With 200k unrelated (non-fail) events added, 0 post-claim measured
  2.3 ms (vs 2.1 ms without), so unrelated history did not slow the query:
  `uv run python apps/api/bench/dead_session_recovery_bench.py --post-claim 0 --unrelated 200000`

  Control for other-reason fails: `--other-fails N` adds N `task.fail` events
  (`reason` `channel_error`) in the window. They are scanned through
  `events_kind_at` and rejected by the reason predicate, so they are not in the
  cohort but still cost time (1000 `session_lost` fails, 0 post-claim, 1000 rows
  returned in every row). The bench stamps its other-reason fails at NOW, newer
  than every cohort fail, so this control is the worst case for the scan; it is a
  worst-case control for the cap scenario, not a typical window:

  | other-reason fails in window | median ms |
  | --- | --- |
  | 0 | 2.2 |
  | 10000 | 3.5 |
  | 100000 | 19.0 |
  | 300000 | 53.7 |

  Commands, one per row, with `N` = 0, 10000, 100000, 300000:
  `uv run python apps/api/bench/dead_session_recovery_bench.py --post-claim 0 --fails 1000 --other-fails N`

  Combined: `--post-claim 1000 --other-fails 100000` measured 73.7 ms (59.7 ms
  with `--other-fails 0` in the default sweep).

  Coverage: a synthetic fixture on one machine; no production numbers, no timing
  assertion, and production event volume per task and disk are unmeasured. The
  query is not constant time.
- **Bounded reads.** No full scan of `tasks` or `events`: migration
  `0021_fleet_health_indexes.sql` adds `tasks_finished` and `events_kind_at`; per-task
  claim lookups use `events_subject`. Each fetch keeps at most 5000 newest rows and
  sets `truncated` when it hits that. For dead-session recovery the cap bounds
  the scan to the newer-than-cutoff range; other-reason fails in that range still
  cost time.

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
