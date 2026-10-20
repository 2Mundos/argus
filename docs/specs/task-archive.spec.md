# Task archive

# 1. Purpose and Scope

Terminal tasks (`succeeded`, `failed`, `cancelled`) never leave the database, so
boards, lists and totals fill with finished work. Operators can **archive** a
terminal task: it disappears from the default task list and from every
aggregate, stays fully readable, and can be brought back. See
[worker lifecycle](worker-lifecycle.spec.md) 3.2 for the states.

# 2. Non-Goals

- No deletion. Rows, attempts, events, artifacts and results are kept; there is
  no `DELETE /tasks/{id}`.
- Archiving is not a state and not a transition: workers cannot trigger it and
  it never changes `state`.
- No automatic archiving by age. Operators choose, explicitly.
- Archived tasks are not hidden from dependency logic: a `pending` task whose
  dependency is archived still sees that dependency's real state.

# 3. Decisions Made

- **Storage.** One additive column, `tasks.archived_at` (NULL: not archived;
  migration `0019_tasks_archive.sql`). It is exposed on every task.
- **Archive one:** `POST /tasks/{id}/archive` (manager scope) accepts `succeeded`,
  `failed` and `cancelled` tasks and returns the task. Any other state
  (`awaiting_approval`, `pending`, `ready`, `claimed`, `blocked`, `in_review`)
  answers 409 `task_not_terminal` naming the state, and changes nothing.
  Archiving an archived task succeeds without any change and writes no event.
- **Unarchive:** `POST /tasks/{id}/unarchive` (admin scope only) clears
  `archived_at`; idempotent likewise.
- **Bulk:** `POST /tasks/archive` (manager scope) with `states` (required,
  terminal only), optional `project`, optional `older_than_days` (matches tasks
  whose last change, `updated_at`, is older), and `dry_run`. It archives every
  matching unarchived task in one transaction and answers `{count, dry_run}`.
  With `dry_run: true` nothing is written and `count` is what would be archived.
  A non-terminal state is refused with 422 `state_not_archivable` before
  anything changes.
- **Events.** Each archive and unarchive that changes a task writes
  `task.archived` / `task.unarchived` with the actor.
- **Listing.** `GET /tasks` excludes archived tasks; `archived=true` lists only
  archived ones (combinable with the other filters). Reads by id (`GET
  /tasks/{id}`, `ids=`) always find archived tasks.
- **Aggregates.** Task counts by state in the overview (Home, kanban totals),
  queue health in operations, and Prometheus task gauges exclude archived tasks.
  Only terminal tasks can be archived, so active-work figures are unaffected.
- **Retry.** Retrying an archived `failed` task clears `archived_at`; an archived
  task is never active.
- **Dashboard.** Terminal task details offer Archive (Unarchive when archived).
  The task board has an "Archived" toggle, a bulk "Archive…" dialog (state,
  project, age) that shows the count from a dry run and requires confirmation
  before archiving.

# 4. Open Questions

- Should archived tasks be purged after a retention period? Not now; it needs a
  spec of its own (deletion is destructive).

# 5. Constraints

- Additive migration only; no row, attempt or result is deleted or rewritten.
- All writes go through `Database.write()` and use `tx.now`.

# 6. Implementation Consequences

- `tasks.views.list_tasks` filters on `archived_at`; `overview` and `ops`
  filter their task counts the same way.
- New aggregates over `tasks` must exclude `archived_at IS NOT NULL`.
