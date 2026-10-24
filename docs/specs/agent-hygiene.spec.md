# Agent hygiene: lost/gone aggregates, bounded archiving and alerts

# 1. Purpose and Scope

Lost and gone agent identities stay in `agents` forever, so the table only grows
and neither the Home totals nor the Agents tab says *where* and *how long ago*
those identities died. This spec adds:

1. **Aggregates.** `GET /api/agents/hygiene` groups `lost` and `gone` agents by
   pool and by day, reports the age of their last heartbeat, and counts the lost
   agents that still hold a `claimed` task.
2. **Bounded, opt-in archiving.** An admin can archive `lost`/`gone` identities
   older than N days, in bounded batches, so they leave lists and aggregates
   while the rows stay readable.
3. **An alert.** `GET /api/overview` reports two explicit, testable signals: a
   lost agent that still holds a claimed task, and an abnormal jump in newly
   lost agents against the immediately preceding window.
4. **Parity.** The same groups and alerts are reachable over MCP and shown on
   the dashboard (Home attention list and the Agents tab).

Nothing here restarts a worker, recycles an instance, or performs operational
cleanup: this is read-only reporting plus an explicit admin archive.

# 2. Non-Goals

- No deletion. Archiving sets a timestamp; there is no `DELETE /agents/{id}` and
  no retention/purge job.
- No automatic archiving by age. The threshold is a configured default; an
  archive still has to be requested.
- No change to how an agent becomes `lost` (`agent-reaper`, worker lifecycle
  3.4), to task states, to leases, or to existing events.
- No new external service, no history charts, no per-minute time series.
- No restart, recycle, drain or instance action is added or triggered by an
  alert.
- Home keeps its rule that a number is a measured count, not a health verdict.

# 3. Decisions Made

- **`archived_at` on `agents`.** One additive nullable column
  (`0044_agents_archive.sql`), the same shape as `tasks.archived_at`
  ([task archive](task-archive.spec.md)). Archiving is not a state and never
  changes `state`, `ended_at`, `end_reason` or `last_heartbeat_at`.
- **Pool attribution.** An agent's pool is its instance's pool
  (`agents.instance_id → instances.pool`). An agent with no instance, or whose
  instance row no longer exists, is grouped under the literal pool name
  `unknown`; it is never dropped from the totals.
- **Age is heartbeat age.** Age is `as_of - last_heartbeat_at`, not the time
  since `ended_at`: it is the quantity that says how stale the identity was.
  Buckets are `under_1d`, `1d_to_7d`, `7d_to_30d`, `30d_and_over`, half-open on
  the left (`[0,1)`, `[1,7)`, `[7,30)`, `[30,∞)` days).
- **Groups.** One group row per `(pool, state, day)` with `day` the UTC calendar
  date of `ended_at`, plus `count`, `oldest_heartbeat_at`,
  `newest_heartbeat_at` and `max_age_hours`. Rows are ordered by pool, then
  state, then day descending.
- **Totals.** `totals` reports `lost`, `gone`, `archived` (rows with
  `archived_at` set, of either state) and `lost_holding_claims`, each over the
  whole table regardless of instance presence.
- **Lost that hold a claimed task.** The query is
  `tasks.state = 'claimed' AND assignee_agent_id = <agent>` joined to agents in
  `lost`. It must normally be 0 — ending an agent expires its leases — so a
  non-zero value is a real inconsistency, and the endpoint also returns the
  pairs (`agent_id`, `task_id`) so an operator can see which ones.
- **Alert: lost holding a claimed task.** Severity `critical`, count = number of
  such agents (not tasks). Always on; no threshold.
- **Alert: abnormal jump.** Window and threshold are explicit and configurable:
  - `ARGUS_LOST_SPIKE_WINDOW_HOURS` (`lost_spike_window_hours`, default `24`)
  - `ARGUS_LOST_SPIKE_MIN_COUNT` (`lost_spike_min_count`, default `5`)
  - `ARGUS_LOST_SPIKE_FACTOR` (`lost_spike_factor`, default `2.0`)
  Current count is agents whose `ended_at` falls in `(as_of - window, as_of]`;
  previous count is the equal window immediately before it. The signal triggers
  when `current >= min_count AND current > factor * previous`. Severity
  `attention`. The payload carries `window_hours`, `current`, `previous`,
  `min_count`, `factor` and `triggered`, so the rule is auditable from the
  response and testable without recomputing it.
- **Overview shape.** `agents` gains
  `lost_holding_claims: int`, `lost_spike: {window_hours, current, previous,
  min_count, factor, triggered}` and
  `[state, pool, bucket] → count` maps (`hygiene_by_pool`, for `lost` and `gone`
  alike). `by_state` excludes archived agents; the `archived` total is reported
  separately so nothing becomes invisible.
- **Archiving is bounded and opt-in.**
  - `POST /api/agents/archive` (admin scope only) takes `states` (non-empty
    subset of `lost`, `gone`), optional `older_than_days` (0 means any age),
    optional `limit` (1…1000, default 200) and `dry_run`.
  - Default `older_than_days` comes from `ARGUS_AGENT_ARCHIVE_AFTER_DAYS`
    (`agent_archive_after_days`, default unset = archiving disabled): with no
    value and no explicit `older_than_days`, the endpoint answers 409
    `agent_archive_disabled` and changes nothing. This is the opt-in.
  - The cutoff is `ended_at < as_of - older_than_days`; live agents are never
    eligible and a non-`lost`/`gone` state is refused with 422
    `state_not_archivable` before anything changes.
  - At most `limit` rows are archived per call, oldest `ended_at` first, so a
    caller can page through a large backlog deliberately.
  - `dry_run: true` returns the same `count` and writes nothing.
- **Archiving touches nothing else.** It writes no `agent.archived` event and
  changes no task, lease or existing event row. This deliberately differs from
  [task archive](task-archive.spec.md), because an agent archive is bookkeeping
  over already-ended identities and the task requires the events table to stay
  untouched.
- **Listing and reads.** `GET /api/agents` excludes archived agents;
  `archived=true` lists only archived ones. `GET /api/agents/{id}` and the
  heartbeat/leave paths are unaffected and always find the row.
- **MCP.** `get_agent_hygiene` (managers) forwards to
  `GET /api/agents/hygiene`; `archive_agents` (admins) forwards to
  `POST /api/agents/archive`. Both are thin calls, like every other tool.
- **Dashboard.** Home shows two attention items (lost holding a claimed task as
  critical; abnormal jump as attention, naming window, current and previous) and
  the Agents tab shows a "Lost and gone" table per pool with the age buckets,
  the totals and the same two alerts. Guards: nothing renders while the data is
  loading, and an unknown source is never rendered as zero.

# 4. Open Questions

- Should an archive record who did it? It would have to be an event row, which
  this spec deliberately does not write. Revisit if the events table may grow
  for bookkeeping.
- Should a purge after retention exist? No: deletion is destructive and needs a
  spec of its own.
- Is `factor * previous` the right shape for "abnormal" once there is a season
  pattern (nights, weekends)? Not now; the knobs are explicit so it can change
  without touching callers.

# 5. Constraints

- Single node, SQLite only: one additive migration, no new service or
  dependency.
- Hygiene reads must not take the write lock; `overview` keeps its one-snapshot
  read transaction and its zero/unknown/error distinctions.
- No secrets: the payload carries pool names, counts, enum values, timestamps
  and agent/task ids only — never a token, credential or free text.
- English only, including UI copy, tests and commit messages.
- Archiving must be safe for a backlog of any size: bounded `limit`, oldest
  first, idempotent (already archived rows are never counted again).

# 6. Implementation Consequences

- Migration `apps/api/src/argus_api/db/migrations/0044_agents_archive.sql` adds
  `agents.archived_at`; every new aggregate over `agents` must decide explicitly
  whether it includes archived rows (`GET /api/agents`, `overview.by_state`,
  `agents/hygiene.totals` exclude; `archived` total and archived listing
  include).
- Backend: `apps/api/src/argus_api/agents/hygiene.py` (aggregates and the spike
  rule), `agents/service.py` (`archive_matching`), `agents/routes.py`
  (`GET /agents/hygiene`, `POST /agents/archive`), `overview.py` (new fields and
  the two signals), `config.py` (three spike knobs and the archive default),
  `mcp_server.py` (two tools).
- Tests: `apps/api/tests/test_agent_hygiene.py` (grouping by pool/day/bucket,
  `unknown` pool, archived exclusion, lost-holding-claim detection, spike rule
  at its boundary with a fake clock, archive opt-in/bounds/no-side-effects,
  scopes), plus the MCP journey test and the overview tests.
- Frontend: `apps/dashboard/src/views/Agents.tsx` (hygiene section),
  `apps/dashboard/src/overview.ts` and `views/Home.tsx` (attention items),
  Vitest coverage next to each.
- After `make openapi`, `packages/api-client/src/schema.d.ts` carries the new
  routes and models; `make openapi-check` must pass.
