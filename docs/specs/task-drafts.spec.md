# Task drafts

# 1. Purpose and Scope

`create_task` makes a task claimable in the transaction that creates it. An
operator or orchestrator assembling several related tasks therefore races the
fleet: a task can be claimed before its dependencies, pins or acceptance
criteria are added by a later `edit_task`/`set_task_dependencies`, and once
it is claimed most of its definition is immutable ([task edit](task-edit.spec.md)
3.4). Creating dependents first is impossible, because a dependency must exist.

This spec adds **task drafts**: persisted, editable task definitions that are not
tasks. A draft is never listed, counted, claimable or eligible; it becomes a task
only through an explicit **publish**, which validates the whole batch and creates
every task with its complete definition and real dependency edges in one write
transaction.

Surfaces, all calling one module, `tasks.drafts`:

- REST under `/api/task-drafts` (3.6);
- MCP `create_task_draft`, `list_task_drafts`, `get_task_draft`,
  `edit_task_draft`, `discard_task_draft`, `publish_task_drafts`, thin calls to
  REST like every other tool ([MCP operations](mcp-operations.spec.md) 3.4).

`POST /api/tasks` and MCP `create_task` are unchanged and stay the direct path.

# 2. Non-Goals

- No change to `create_task`, `edit_task` or the dependency rules of existing
  tasks; no new required field for tasks in general (no repository or coding
  fields): a non-coding task drafts and publishes like any other.
- No arbitrary cancellation, provider pins policy or deployment changes.
- No dashboard editor in this spec; drafts are a REST/MCP surface first.
- No per-project or per-author permissions (none exist,
  [knowledge](knowledge.spec.md) 3.1): every manager sees every draft.
- No "unpublish": a published task is a task, changed with `edit_task` or cancelled.

# 3. Decisions Made

## 3.1 Draft

A draft is a row of `task_drafts` (migration `0065_task_drafts`):
`id`, `body` (JSON), `created_by`, `created_at`, `updated_by`, `updated_at`,
and, once published, `published_at`, `published_task_id`, `publication_key`.

- `body` takes every `TaskCreate` field, with the same per-field rules (lengths,
  ranges, enums, providers), plus `depends_on_drafts`: ids of other drafts. Saving
  is lenient about *completeness* only: `title` may be empty and `spec`,
  `acceptance` and `project` missing until publish. Unknown keys are refused
  (`422 validation_failed`).
- `depends_on` holds ids of **existing tasks**; `depends_on_drafts` holds ids of
  **drafts**. They are separate fields, so an id is never guessed to be one or
  the other. Together they are at most 100 (task dependencies 3.1).
- `version` is the draft's `updated_at`. Every successful edit yields a new,
  strictly greater version.
- A draft is read back whole with `GET /api/task-drafts/{id}`. A published draft
  stays as the record of what was published and carries `published_task_id`.
- Drafts survive restarts: they are rows in the database like tasks.
- Drafts are never in `tasks`, so no claim, list, inbox, overview, metric,
  routine or reaper ever sees them. Workers cannot read them either (3.5).

## 3.2 Lifecycle

```
create ──▶ open ──edit (expected_version)──▶ open ──publish──▶ published
             └──discard (expected_version)──▶ (row deleted)
```

- **Edit** (`PATCH`) takes `{expected_version, changes}`: a partial patch of
  `body` fields. A field left out is kept; `null` resets it to its default; a
  list is replaced whole. The merged body is checked with the save rules. A
  mismatched `expected_version` is `409 stale_version` with the current version
  in `details`; nothing changes. A patch that changes nothing writes nothing
  and keeps the version.
- **Discard** (`DELETE ?expected_version=`) removes an open draft. Version rules
  as for edit. Another open draft that still names it in `depends_on_drafts`
  then fails publish validation until it is edited.
- A published draft refuses edit and discard with `409 draft_published`.
- At most **200 open drafts** exist at once (`409 too_many_drafts`), so the
  list is always whole.

## 3.3 Publish

`POST /api/task-drafts/publish` with:

```
{ "idempotency_key": "…", "mode": "atomic" | "partial",
  "drafts": [{"draft_id": "…", "expected_version": "…"}, …] }
```

- 1–**50** drafts per call, each named once. `idempotency_key` is 1–200
  characters and required. `mode` defaults to `atomic`. **Both REST and MCP
  offer both modes**; MCP's `publish_task_drafts` takes the same `mode`
  argument with the same default.
- **Validation** of each draft, all inside the write transaction, before any
  task is written:
  - the draft exists (else `not_found`), is open (else `draft_published`) and is
    at `expected_version` (else `stale_version`);
  - the body is a valid `TaskCreate` (title 1–200 characters, …);
  - **completeness**: a non-blank `spec` and at least one acceptance criterion,
    none blank (`validation_failed`). Only drafts require this; direct create
    does not;
  - the kind exists and its input schema holds; the project exists (a draft
    without `parent_id` must name a real project, not `global`,
    `project_required`); pinned pages exist;
    the harness account pin resolves (the create rules, `check_create`);
  - **capabilities**: every required capability (after the kind's defaults) is
    declared by at least one pool or one non-archived agent
    (`unknown_capability`); a capability nobody can serve would leave the task
    `ready` forever;
  - **dependencies**: every `depends_on` task exists (`not_found`); every
    `depends_on_drafts` id is in this batch or names a draft that is already
    published (then its `published_task_id` is used). Anything else is
    `validation_failed` (`depends_on_drafts: <id> is not in this batch`).
    Edges among the batch must form no cycle, a self reference included
    (`dependency_cycle`, the path in `details`). New tasks have no dependents,
    so no cycle can pass through an existing task;
  - the caller's limits: the same per-agent hourly policy as `create_task`
    ([worker lifecycle](worker-lifecycle.spec.md) 3.10) applies to each task
    created; past it, the task waits in `awaiting_approval` with a `policy`
    decision instead of being refused.
- **Creation** runs in topological order of the batch's edges, through the same
  `tasks.service.create_task` as direct create: in-batch references are rewritten
  to the real new task ids, so each new task's dependencies exist and its state
  follows the usual rule (`ready` with no unfinished dependency, else `pending`;
  `awaiting_approval` with `requires_approval`). The draft is marked published
  with its task id and records `task_draft.published`. The tasks carry the
  ordinary `task.created` events.
- **Atomic** (`mode: atomic`): if any draft fails, nothing is written and the
  call fails with one error: `404 not_found` if a draft is missing, else
  `409 stale_version` if one is stale, else `409 draft_published`, else
  `422 dependency_cycle`, else `422 validation_failed`. `details` gives each
  failing item as `<draft_id>: <reason>`, so the outcome per item is explicit.
- **Partial** (`mode: partial`): every draft that passes is published, the
  others are not. A draft whose in-batch dependency was not published is not
  published either (`skipped`). The call answers `200` with an outcome per
  item.
- The answer (both modes, on success) is
  `{idempotency_key, mode, replayed, items: [{draft_id, outcome, task_id, state,
  code, errors}]}` in request order, `outcome` one of `published`, `rejected`
  (with `code` and `errors`) or `skipped` (with `errors` naming the draft it
  waited on).

## 3.4 Idempotency, retries and concurrency

- A successful publish stores `{key, request digest, answer}` in
  `task_draft_publications` in the same transaction. Repeating the same key with
  the same request (same mode, ids and versions) returns the stored answer with
  `replayed: true` and writes nothing: no duplicate task, even when the first
  answer was lost. The same key with another request is
  `409 idempotency_key_reused`.
- A refused atomic publish stores nothing (nothing happened); a retry with the
  same key is evaluated again.
- Publishing a draft that another call already published is `draft_published`
  for that item: a draft becomes at most one task, whatever the keys.
- **Publish versus claim.** Writes are serialized by the single writer
  ([single node](single-node.spec.md)). Tasks, edges, pins and the draft marks are
  inserted in one transaction, and readers see only committed data, so a claim
  sees either none of the batch or all of it with its complete definition and
  dependencies. A dependent can never be claimed before its in-batch
  dependency's edge exists.
- **Edit versus publish.** Publish checks each draft's `expected_version` inside
  the transaction: an edit that commits first makes the publish stale; a publish
  that commits first makes the edit `409 draft_published`.

## 3.5 Permissions

Every draft endpoint and tool is orchestrator or admin only; workers get
`403 forbidden` on REST and the tools are not listed to them. A draft may hold an
unreviewed spec, so reads are restricted too. `403` is decided before the draft
is looked up. Actors are recorded as `admin:<label>` or `agent:<id>`.

## 3.6 Interfaces and errors

| REST | MCP | Notes |
| --- | --- | --- |
| `POST /api/task-drafts` (body fields) | `create_task_draft` | `201`, the draft |
| `GET /api/task-drafts` | `list_task_drafts` | open drafts, oldest first, `{items, total}` (whole: ≤ 200) |
| `GET /api/task-drafts/{id}` | `get_task_draft` | open or published |
| `PATCH /api/task-drafts/{id}` | `edit_task_draft` | 3.2 |
| `DELETE /api/task-drafts/{id}?expected_version=` | `discard_task_draft` | `204` |
| `POST /api/task-drafts/publish` | `publish_task_drafts` | 3.3; `atomic` or `partial` |

Errors keep the shape `{detail: {code, message, details}}` on REST and
`<status> <code>: <message> | <details>` on MCP.

| Status | `code` | When |
| --- | --- | --- |
| 403 | `forbidden` | not an orchestrator/admin token |
| 404 | `not_found` | draft (or, atomic, a dependency) does not exist |
| 409 | `stale_version` | `expected_version` is not current |
| 409 | `draft_published` | the draft was already published |
| 409 | `too_many_drafts` | 200 open drafts exist |
| 409 | `idempotency_key_reused` | the key was used for another request |
| 422 | `validation_failed` | a value breaks a rule; `details` per field or item |
| 422 | `dependency_cycle` | the batch's edges close a cycle |

# 4. Open Questions

- A dashboard draft editor, once operators ask for it.
- Whether old published drafts should be pruned; they are small and kept as the
  record for now.

# 5. Constraints

- One write transaction per operation, `tx.now` for time, no network call in it.
- One domain module for REST and MCP; MCP holds no rules (worker lifecycle 3.7).
- The schema change is additive (`task_drafts`, `task_draft_publications`) and
  written with `IF NOT EXISTS`, so it is idempotent.
- No secrets in events: `task_draft.published` carries ids only.

# 6. Implementation Consequences

1. This spec, its index link, and pointers in worker lifecycle (3.2, 3.6, 3.7),
   task edit, task dependencies and MCP operations.
2. Migration `0065_task_drafts.sql`; `argus_core.task_drafts` (body, patch and
   publish models); `argus_api.tasks.drafts` and its routes.
3. The six MCP tools and the orchestrator skill.
4. OpenAPI and the TypeScript client regenerated.
5. Tests in `apps/api/tests/test_task_drafts.py`.

## Verification boundary

- Commands: `make check`; `uv run pytest apps/api/tests/test_task_drafts.py`.
- Verified (unit and in-process API/MCP): nothing is claimable before publish;
  idempotent retries; dependency ids and edges; cycles; invalid payloads; publish
  against concurrent claims; persistence across reopen; stale edits;
  permissions; the migration upgrade from 0064 and its idempotence.
- Not verified: behaviour on a production database under real fleet load.
