# Task edit

# 1. Purpose and Scope

An operator or orchestrator who spots a wrong title, a thin spec, a missing
acceptance criterion or a forgotten prerequisite can only cancel and recreate
the task today (only dependencies are editable, [task
dependencies](task-dependencies.spec.md) 3.1). This spec adds **editing an
existing task's definition** with the same behaviour on every surface:

- REST `PATCH /api/tasks/{id}`;
- MCP `edit_task`;
- the dashboard's Edit action on the task detail.

All three call one function, `tasks.service.edit_task`, which owns every rule
below. `PUT /api/tasks/{id}/dependencies` and `set_task_dependencies` stay, as
thin callers of the same function with only `depends_on` set.

# 2. Non-Goals

- No change to creation, retry, cancel, approval, archive or PR management.
- No editing of a running attempt: an edit never reaches a worker that already
  holds the task (3.4).
- No edit history browser in the UI beyond the existing History list; the audit
  is the `task.edited` event (3.7).
- No per-project permissions (none exist, [knowledge](knowledge.spec.md) 3.1).

# 3. Decisions Made

## 3.1 Fields

**Editable** (the only keys `changes` accepts):

| Field | Type | Rules (same as `TaskCreate`) |
| --- | --- | --- |
| `title` | string | 1–200 characters; cannot be cleared |
| `spec` | string | any length; `null` or `""` clears it to `""` |
| `acceptance` | list of strings | `null` or `[]` clears |
| `priority` | integer | cannot be cleared (send `0` to reset) |
| `required_capabilities` | list of strings | de-duplicated and sorted, as at creation; `null` or `[]` clears |
| `knowledge` | list of page ids | replaces the pinned set; every page must exist and belong to the task's project or to `global` (`422 knowledge: …`); `null` or `[]` unpins all. Pins a *kind* adds at creation are not re-added |
| `complexity` | `trivial` \| `standard` | cannot be cleared; sets `complexity_source` to `explicit` ([task complexity](task-complexity.spec.md)) |
| `depends_on` | list of task ids | replaces the set; the rules of [task dependencies](task-dependencies.spec.md) 3.1: ids exist, no self-dependency, no cycle, at most 100 |
| `account_providers` | list of providers | engine providers only, de-duplicated and sorted, as at creation ([local harness](local-harness.spec.md) 3); `null` or `[]` clears (no restriction) |

**Immutable**: `state`/`status`, `assignee_agent_id`, `lease_expires_at`,
`lease_ttl_s`, `timeout_s`, `timeout_at`, `attempt`, `failures`,
`max_attempts`, `result`, `error`, `block_reason`, `progress_*`, `input`,
`review`, `requires_approval`, `created_*`, `updated_at`, `archived_at`, any
event or history, and the structural ones `project`, `parent_id`, `kind`
(and `id`). Naming any of them (or any unknown key) in `changes` is
`422 immutable_field`, nothing is written, and `details` lists the offending
keys. They are not options of the MCP tool or of the dashboard form; the
schema documents only the editable fields. State changes go through their own
operations (approve, cancel, retry, …).

## 3.2 Patch semantics

- `changes` is a partial patch: a key that is **omitted is preserved**.
  An empty `changes` is `422 validation_failed` (`changes: nothing to change`).
- `null` is the explicit **clear** for `spec`, `acceptance`,
  `required_capabilities`, `knowledge` and `depends_on` (equivalent to `""` or
  `[]`). `null` for `title` or `priority` is `422`.
- A list is replaced as a whole, never merged.
- A patch whose values all equal the current ones is a no-op: nothing is
  written, no event, the version does not change, the current task comes back.
- Validation is **atomic**: the patch is checked as a whole inside the write
  transaction, before the first write. One bad field rejects the edit and the
  task is untouched.

## 3.3 Version precondition

The request carries `expected_version`, the `version` the caller read
([MCP operations](mcp-operations.spec.md) 3.1). A mismatch is
`409 stale_version` with the current version in `details`; nothing changes. A
successful edit that changed something always yields a new, strictly greater
`version`, even within the clock's resolution. The response is the task, with
its new `version`, and `get_task`/`get_tasks` read back the same version and
values immediately.

## 3.4 What each state permits

The single source of truth is `argus_core.task_edit.EDITABLE_BY_STATE`. The API
returns the allowed set as `editable_fields` on every task; the dashboard form
and the MCP tool description are driven by it.

| State | Editable fields | Why |
| --- | --- | --- |
| `awaiting_approval`, `pending`, `ready` | all seven | nothing has run; the next claim sees the saved definition |
| `blocked`, `claimed`, `in_review` | `priority` | queue order only; the title, spec, criteria, capabilities, pins or dependencies may already be in use by an attempt or a reviewer |
| `succeeded`, `failed`, `cancelled` | none | the record of what ran stays as it was |
| archived (any) | none | archived tasks are read-only |

A change outside the state's set is `409 task_not_editable`; `details` lists
`state: <state>`, `not editable: <fields>` and `editable: <fields>`. We block;
there is no "edit now, acknowledge later" path.

**Edit versus claim.** Writes are serialized, so the two outcomes are the only
ones: an edit that commits first is seen by the claim (the worker gets the new
definition); a claim that commits first changes the task's `version` and state,
so the edit fails `409 stale_version` (or `task_not_editable` if it was
prepared against the new version). An edit never changes a task after a worker
holds it, and Argus never assumes it updates a running attempt's prompt: a
claimed task keeps working on the definition it was handed.

## 3.5 Attempt snapshot

At every claim Argus stores the definition the worker is given in
`task_attempts` (`task_id`, `attempt`, `agent_id`, `claimed_at`, `definition`:
the seven editable fields plus `kind`, `project`). The definition is never
updated and rows are never deleted; an operator release only sets the row's
`outcome` (`released`) and `outcome_reason` (migration 0031,
[worker lifecycle](worker-lifecycle.spec.md) 3.3); `attempt` restarts at 1 after a retry, so they are listed in claim order. `GET /api/tasks/{id}/attempts` lists them, oldest first, so the
definition of any attempt can be told apart from a later edit.

## 3.6 Dependent state

After `depends_on` changes the task follows the existing logic in the same
transaction ([task dependencies](task-dependencies.spec.md) 3.1): a `ready`
task with an unfinished dependency goes to `pending`; a `pending` one whose
dependencies all `succeeded` becomes `ready`; `awaiting_approval` keeps its
state. `task.dependencies_changed` and `task.dependency_failed` events are
raised as before.

## 3.7 Audit

Every edit that changes something records one `task.edited` event in the
transaction: actor (`admin:<label>` or `agent:<id>`), time, `from_version`,
`to_version` and `changes`, a diff per changed field:

- `title`, `priority`, `complexity`: `{from, to}`;
- `required_capabilities`, `knowledge`, `depends_on`: `{added, removed}` ids;
- `spec`, `acceptance`: only lengths/counts and a SHA-256 digest of the old and
  new text. The text itself is never copied into the event log, which is
  streamed and shown in History: whatever an operator pasted into a spec, a
  secret included, does not spread. The text of what each attempt ran is in the
  attempt snapshot (3.5).

## 3.8 Permissions

Orchestrator and admin tokens edit; worker tokens get `403 forbidden` on REST
and on MCP (the tool is not listed to them). This is the same authorization as
`set_task_dependencies`; nothing is widened. There are no per-project
permissions: project and kind cannot change, a knowledge page of another
project cannot be pinned, and error messages never echo another project's page
content. `403` is decided before the version, state or content is looked at.

## 3.9 Errors

Domain errors keep the shape `{detail: {code, message, details}}` on REST and
`<status> <code>: <message> | <details>` as a tool error on MCP.

| Status | `code` | When |
| --- | --- | --- |
| 403 | `forbidden` | not an orchestrator/admin token |
| 404 | `not_found` | task, dependency or pinned page does not exist |
| 409 | `stale_version` | `expected_version` is not current; `details`: `[current version]` |
| 409 | `task_not_editable` | a field is outside the state's set (3.4) |
| 422 | `immutable_field` | an immutable or unknown key (3.1) |
| 422 | `validation_failed` | a value breaks a rule; each `details` entry is `<field>: <why>` |
| 422 | `dependency_cycle` | self or transitive dependency; `details`: the path |

## 3.10 Dashboard

The task detail shows **Edit** when `editable_fields` is not empty. The form is
prefilled from the task, fields outside `editable_fields` are disabled, and
Save sends only the fields the operator changed with the task's `version`.
Per-field errors (`details` entries `<field>: …`, `dependency_cycle` and
missing dependencies on *Depends on*) show beside their field. On
`stale_version` the form stays open with the operator's draft untouched, says
who is ahead, and offers *Reload latest* (discard the draft) or *Keep editing
on the latest version* (re-base the draft on the current version). A re-based
draft that changes a field the new state does not allow shows that field's
error instead of dropping the change. Live
refreshes never overwrite a draft. Cancel closes without a request.

# 4. Open Questions

- Whether `claimed` tasks should accept a spec edit delivered to the running
  worker (a clarification-like message). Not without a worker-side contract.
- Whether kind pins should be re-merged on edit.

# 5. Constraints

- One write transaction per edit, `tx.now` for time, no network call inside it.
- One service function for all surfaces; REST, MCP and UI hold no rules of their
  own and the MCP tool forwards to REST like every other tool.
- The only schema change is the additive `task_attempts` table (migration 0024).
- No secrets in the audit (3.7).

# 6. Implementation Consequences

One PR:

1. This spec, its index link, and pointers in task dependencies,
   MCP operations and worker lifecycle (3.6, 3.7).
2. Migration `0024_task_attempts.sql`; `argus_core.task_edit` (fields, state
   policy, patch model); `Task.version` and `Task.editable_fields`.
3. `tasks.service.edit_task` (and `set_dependencies` delegating to it), the
   claim snapshot, `PATCH /api/tasks/{id}`, `GET /api/tasks/{id}/attempts`.
4. MCP `edit_task`, instructions and tool catalog; OpenAPI and the TypeScript
   client regenerated.
5. Dashboard form, helpers and tests.
6. Tests: parity (REST, MCP over the protocol, readback), atomic validation,
   dependent state, concurrency and the edit/claim race, per-state policy,
   permissions and immutables on both surfaces, snapshots, audit, and
   dashboard component tests. Browser screenshots under `docs/evidence/` need
   Playwright, which this repo does not have; they are not part of this PR.
