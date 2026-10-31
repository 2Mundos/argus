# Routines

# 1. Purpose and Scope

A **routine** is recurring work on a schedule: a cron expression plus a task
template, owned by a project. When it is due, Argus creates a task from the
template; workers claim it like any other task. A routine run may report findings
that become new tasks.

Example: project `radar`, routine `error-log`, every hour: "Read the production
logs since the last run and identify live errors. For each new error, propose a
task." The run reads the logs, and Argus files one task per new error,
deduplicated across runs.

Readable by any valid token. Managed by orchestrators and admins.

# 2. Non-Goals

- No separate scheduler service, queue or cron daemon: one in-process loop
  ([single node](single-node.spec.md) 3.1).
- No sub-minute schedules and no precise timing: a run starts within one loop tick
  of its due time.
- No backfill: a missed window is run once, not once per missed slot.
- No new way to execute work. A run is an ordinary task; leases, accounts, models,
  review and failure classes apply unchanged.
- No credentials in routines. Access to production logs comes from the pool that
  has the capability (`required_capabilities`), as for any task.

# 3. Decisions Made

## 3.1 Data model

Table **`routines`**: `id`, `project` (FK), `slug` (unique per project), `title`,
`schedule` (5-field cron or `@hourly` | `@daily` | `@weekly`), `timezone` (IANA,
default `UTC`), `template` (json), `findings` (`off` | `approval` | `auto`),
`enabled`, `next_run_at`, `last_run_at`, `last_task_id`, `created_by`, timestamps.

`template` holds the task fields a creator may set today: `kind`, `title`, `spec`,
`acceptance`, `input`, `required_capabilities`, `priority`, `review`, difficulty
and model pins, plus the finding intake's `dedup_window` and `max_findings` (3.3),
which stay out of the run task. It is validated with the task create rules when the routine is
saved, so a bad template fails on save, not at 3 a.m.

## 3.2 Scheduler

- One asyncio loop, every 30 s. In one write transaction it selects enabled
  routines with `next_run_at <= tx.now`, creates their run tasks, and sets
  `next_run_at` to the next slot after `tx.now` (so downtime gives one catch-up run).
- **Overlap**: if `last_task_id` is not terminal, the run is skipped and
  `routine.skipped {reason: previous_running}` is recorded. Runs never pile up.
- A run task has `created_by: routine:<id>`, `prompt_trust: orchestrator` (the
  template was written by a manager), `input.routine = {id, slug, run_at,
  previous_run_at}`, and the title suffixed with the run time.
- Events: `routine.created`, `routine.updated`, `routine.run {task_id}`,
  `routine.skipped`, `routine.disabled`.
- Global setting `routines.enabled` ([settings](settings.spec.md)), default `true`,
  pauses every routine without editing them.

## 3.3 Findings

A run reports findings in its result data:

```json
{ "findings": [ { "key": "...", "title": "...", "spec": "...", "acceptance": [...], "priority": 0 } ] }
```

- `key` is a stable fingerprint the run computes (for `error-log`: the error class
  plus the normalized top frame). Argus files a finding only if no task in the
  project with the same `(routine, key)` is non-terminal or was closed in the last
  `dedup_window` (template field, default 7 days). A repeat adds a
  `routine.finding_seen {task_id}` event to the existing task instead.
- Finding tasks are children of the run task with `input.routine_finding = {routine,
  key}`, in the routine's project.
- **Trust.** Findings come from untrusted data (log lines are attacker-controlled
  text), so finding tasks always get `prompt_trust: worker`, whatever the mode:
  - `off`: findings are stored in the run result only.
  - `approval` (default): tasks start in `awaiting_approval`, as worker children do
    today ([prompt-data isolation](prompt-data-isolation.spec.md)).
  - `auto`: tasks go straight to `ready`. Setting `auto` is admin only.
- At most `max_findings` per run (template field, default 10); the rest are counted
  in `routine.findings_truncated`.

As shipped (migration `0063_routine_findings.sql` indexes the `(routine, key)`
lookup on `tasks.input`):

- Intake runs in the transaction that moves a run task to `succeeded` (after its
  review, if it has one), with the routine's current mode; a failed or cancelled
  run, an `off` routine or a deleted routine files nothing.
- Template fields `max_findings` (integer, 1..100, default 10) and `dedup_window`
  (integer seconds, 60..31536000, default 604800 = 7 days; booleans and strings are
  refused) are validated on save, returned as saved, and never reach a task body.
- Each entry takes `key` (1..200 characters), `title`, `spec`, `acceptance`
  (strings) and `priority` (integer, -1000..1000) and nothing else: other fields
  (`project`, `prompt_trust`, `kind`, `input`, ...) are ignored. The child is a
  `generic` task created by `system` with `parent_id` the run, the routine's
  project and `input = {routine_finding: {routine, key}}`; the task create rules
  apply. Entries are taken in order; a later entry with a key already seen in the
  same result is dropped.
- An entry that is not an object, fails those rules or cannot be created is
  skipped; a run's skips are one `routine.findings_skipped {task_id, skipped:
  [{index, error}]}` on the routine (`index` null when `findings` is not a list),
  with the error location and message but not the value. A malformed result never
  fails the completion.
- Dedup counts only finding tasks Argus created (`created_by: system`), so a task
  an agent creates with `input.routine_finding` suppresses nothing. "Closed" is the
  terminal task's last transition (`updated_at`), compared with `tx.now -
  dedup_window`. `routine.finding_seen {task_id}` is recorded on the existing task
  by `system`, with the repeating run's task id.
- Repeats and skipped entries do not use up `max_findings`. Once it is reached,
  the remaining entries (and any past the first 1000) are not examined:
  `routine.findings_truncated {task_id, count}` on the routine counts them.
- Concurrent completions are serialized by the single writer, so one key yields
  one task.

## 3.4 API, MCP, dashboard

- `GET /api/projects/{slug}/routines`, `GET /api/routines/{id}` (with the last runs).
- `PUT /api/projects/{slug}/routines/{routine}` creates or replaces; `PATCH` for
  `enabled`; `DELETE` removes the routine (its run tasks stay).
- `POST /api/routines/{id}/run` runs it now, still subject to the overlap rule.
- `GET` responses include `next_run_at` and the next three slots, so an operator
  can check a cron expression before it fires.
- MCP: `list_routines`, `get_routine`, `upsert_routine`, `set_routine_enabled`,
  `run_routine`, over the same routes.
- Dashboard: a Routines section per project, listing schedule, next run, last run
  with its task state, findings filed, and enable/run-now actions.

Status: the table (3.1), the scheduler (3.2), run now, finding intake (3.3), the
failure rule (3.5) and the management routes and MCP tools above ship (migrations
`0062_routines.sql`, `0063_routine_findings.sql`), and so does the dashboard
Routines page (`#routines`, per project; "findings filed" reads the last run's `findings_filed`
count from `GET /routines/{id}`, and *unknown* only when the run's count is absent). As shipped: a worker
reads routines only in projects where it holds a lease, like other project reads;
only an admin may set `findings: auto`, replace an `auto` routine or re-enable one,
while managers may disable or delete it; the body's `project`/`slug`, if given,
must match the path; a template's title defaults to the routine's; `next_run_at` is
null while disabled, but the three-slot preview is always computed; `DELETE`
records `routine.deleted`. For runs:

- `run_at` is `tx.now` of the transaction that starts the run; the title suffix is
  that time on the routine's clock, `(YYYY-MM-DD HH:MM <timezone>)`, with the
  title cut to fit 200 characters. `routine.run` and `routine.skipped` carry
  `trigger` (`schedule` | `manual`); a skip also carries `previous_task_id`.
- A scheduled skip still moves `next_run_at` to the next slot. A template that no
  longer passes the create rules at run time (a removed kind) is skipped with
  `routine.skipped {reason: create_failed, error}`, without holding back other
  routines.
- `POST /api/routines/{id}/run` (managers; `run_routine` over MCP) answers `200
  {routine_id, task_id, skipped: null}` when it creates a run, and `200
  {routine_id, task_id: null, skipped: previous_running}` when the overlap rule
  skips it (the skip is recorded too). It works on a disabled routine and while
  `routines.enabled` is off, never changes `next_run_at`, and is `404` for an
  unknown routine and `422` for a template that no longer validates.
- While `routines.enabled` is off the scheduler touches nothing, so turning it on
  gives each due routine one catch-up run.

## 3.5 Failure

A failed run is a failed task, retried by the normal rules. After 3 consecutive
failed runs the routine is disabled with `routine.disabled {reason:
consecutive_failures}` and appears in the inbox.

As shipped: the check runs in the transaction that moves a run task to `failed`.
It counts the routine's latest 3 run tasks created since the routine was last
saved or re-enabled, so a success, an edit or a re-enable starts the count afresh;
a cancelled run breaks the streak. Disabling records `routine.disabled {reason:
consecutive_failures, task_ids}` as `system` and opens a human `policy` decision on
the routine (inbox group `routine:<project>`). Approving it re-enables the routine;
rejecting leaves it disabled; enabling, saving with `enabled: true` or deleting the
routine cancels the open question.

# 4. Open Questions

- "Some orchestrator will pick": phase 1 lets any worker with the capabilities
  claim a run. Should a routine be able to require an orchestrator-role agent
  instead (for runs that plan rather than do)?
- Is a 7-day dedup window right for errors, or should a recurring error after a fix
  reopen (a regression) rather than be suppressed?
- Cron parsing: a small dependency (`croniter`) or a minimal in-repo parser for the
  five fields and the three aliases.
- Should a run see the previous run's result (for "since last run" cursors), or is
  `previous_run_at` enough?

# 5. Constraints

- All writes through `Database.write()`; due-time checks and `next_run_at` use
  `tx.now`; no network call inside the transaction.
- The scheduler only creates tasks; it never reads logs or calls providers itself.
- Task creation by routines counts toward no agent's hourly cap, but each run is
  bounded by `max_findings`, and overlap prevents concurrent runs.

# 6. Implementation Consequences

- Migration `NNNN_routines.sql`: `routines` table and an index on
  `(enabled, next_run_at)`; a unique index on finding tasks' `(routine, key)` lookup
  path (or a small `routine_findings` table).
- `routines` module: validation, slot computation, the loop started from
  `main.py` next to the other loops, finding intake on task completion.
- REST routes, MCP tools, the dashboard section.
- [Worker lifecycle](worker-lifecycle.spec.md) gains `routine:<id>` as a
  `created_by` value; [settings](settings.spec.md) gains `routines.enabled`.
