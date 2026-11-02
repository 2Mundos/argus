# Task deliverable

# 1. Purpose and Scope

A task says what to do (`spec`) and how to tell (`acceptance`), but not what it
must **leave behind**. A worker that ends with a summary and no pull request, or a
PR that was never green, still completes; Argus found out only if someone looked.
And a task could be created that can never deliver what it implies (a PR task with
no repository capability or no repository for its project).

A task gets one **deliverable**: the kind of result that counts as done. It is
validated twice: at **creation**, so a task that cannot reach its deliverable is
refused up front, and at **closure**, so a task is not completed or accepted
without the evidence its deliverable needs.

In scope: the enum and its storage, the creation rules, the completion gate, the
accept gate for results that depend on the forge, editing, binding a pull request
the worker opened but never reported, and every entry point that creates, edits or
closes a task.

# 2. Non-Goals

- **One deliverable per task.** No lists, no combinations (a task may still write
  knowledge or open a PR beyond its deliverable; only the deliverable is checked).
- **No auto-close.** Argus never polls the forge to accept a task. A `pr_green` or
  `pr_merged` task waits for an orchestrator or human, as any reviewed task does
  ([delivery](task-delivery.spec.md) 3.4: merged is not accepted by itself).
- **No forge writes** and no forge calls inside a write transaction.
- **No judgement of quality.** The deliverable proves something exists, not that it
  is good; that stays with `acceptance`, `review` and independent review.
- **No kind default** in this version; the default is `response`.
- **No deployment, issue or release values.** They depend on grants and systems
  Argus does not read.
- No migration of existing tasks beyond the column default (3.2).

# 3. Decisions Made

## 3.1 Values and their evidence

`Deliverable` (`argus_core.tasks`). The default is `response`.

| Value | Counts as done when | Needs at creation |
| --- | --- | --- |
| `response` | the result has a non-empty summary (the envelope already requires one) | nothing |
| `knowledge` | the completing agent published or proposed a knowledge page or revision since this attempt was claimed | nothing |
| `artifact` | the result lists at least one artifact of this task | nothing |
| `subtasks` | the task has at least one child task that is not cancelled | nothing |
| `branch` | a branch artifact observed as pushed (state `open` or `merged`), or any open or merged PR artifact | `repository:write`, a project repository |
| `pr_open` | a PR artifact of the task that is open or merged (from the `pr_opened` milestone or an observation) | same |
| `pr_green` | `pr_open`, and the forge shows exact-head checks `success` (or the PR merged) | same |
| `pr_merged` | `pr_open`, and the forge shows the PR merged | same |

`branch` < `pr_open` < `pr_green` < `pr_merged` is a ladder: each level needs
the previous one's evidence. The first four are independent of it and of each
other.

## 3.2 Where it lives

`tasks.deliverable` (migration `0068`), `TEXT NOT NULL DEFAULT 'response'` with no
`CHECK`, so a new value needs no table rebuild; the API enum is the authority.
`TaskCreate.deliverable` (default `response`), `Task.deliverable`, the claimed
task, and the attempt definition snapshot ([task edit](task-edit.spec.md) 3.5)
carry it. Rows that predate the column read `response`, which is what they were
held to (a summary), so no existing task changes behaviour.

## 3.3 Creation rules

For `branch`, `pr_open`, `pr_green` and `pr_merged`, creation (and a draft publish,
a routine template save and an edit, which share the rules) is refused with
`422` when:

- `repository:write` is not among the task's required capabilities, after the
  kind's defaults: `deliverable_capability`;
- the task's project has no proven repository (the pools' `nanoclaw.repositories`
  map, [delivery](task-delivery.spec.md) 3.1): `deliverable_repository`.

Each error names the deliverable and what is missing. The capability is not added
for the caller. Orchestrators still get an automatic read checkout for configured
projects without setting any capability ([project code
availability](project-code-availability.spec.md)); branch/pr deliverables still
require `repository:write` here. `pr_merged` does not require `delivery.allow_merge`:
a human or orchestrator may merge; the worker's prompt states whether it may.

## 3.4 Completion gate

`complete` checks the evidence of 3.1 that Argus holds inside the transaction.
Missing evidence is `422 deliverable_missing` with `details` naming what to do
(for example "open a pull request, then report `pr_opened`"). Nothing is
recorded and the worker keeps its claim, so it can fix the gap in the same
attempt; if it cannot, it fails or blocks as before. The gate runs before
the checklist and review steps.

`pr_green` and `pr_merged` depend on facts that change after the worker is done.
A completion with an open PR artifact for either goes to `in_review` (a review
decision for the orchestrator is opened even when `review` is `none`) with
`deliverable_pending` in the event, and never to `succeeded`.

## 3.5 Closure gate

Accepting a review decision for a `pr_green` or `pr_merged` task reads the forge
first, **outside** the write transaction (`PrChecker`, as for the delivery
projection), and passes the result in:

- `pr_merged`: the PR must be `merged`.
- `pr_green`: the PR must be `merged`, or `open` with `checks_combined` `success`.
- A PR the forge cannot confirm (no PR found, `unknown` state, unread checks) is
  `409 deliverable_unverified`; a PR that is known but short of the level is
  `409 deliverable_unmet`. Both name the PR state and checks. Argus never
  invents success.
- Rejecting is never gated. An accept that reached the service without a forge
  read is refused as `deliverable_unverified` (fail closed).

The gate applies to every route that answers a decision (single and batch;
the MCP and inbox actions use them). **Independent review** no longer accepts
such a task on its own: on approval it opens the review decision instead, which
then passes the gate above.

## 3.6 Editing

`deliverable` is editable where the capabilities are (ready-like states) and
in `in_review`, where a deliverable that turned out to be unreachable (for
example `pr_green` on a repository without CI) can be lowered. The creation
rules of 3.3 run on the resulting capabilities and deliverable, so removing
`repository:write` from a PR task is refused too. The edit is audited as any other
([task edit](task-edit.spec.md) 3.7).

## 3.7 Binding an unreported pull request

A worker can open a PR and end without reporting `pr_opened` (task `cdd08585`
failed `deliverable_missing` while its PR was open, and the PR later merged).
`POST /api/tasks/{id}/reconcile-pr` (orchestrator or admin) binds such a PR to
the task as its PR artifact, given `repository` (`https://host/owner/repo`),
`number`, the exact 40-character lowercase `head_sha` and a `reason`.

- **Checks before any write.** The repository must be the one the task's project
  proves (3.3; none is `422 deliverable_repository`, another is `422
  pr_wrong_repository`), checked before a credential is used. The PR is then read
  with `PrChecker`, outside any write transaction. A PR the forge cannot confirm
  (not found, unreadable, no head) is `409 pr_unverified`; a head other than
  `head_sha` is `409 pr_stale_head`, naming the forge head. Nothing is guessed.
- **One write.** In one transaction, at `tx.now`: a PR continuation artifact
  (`ref` the number, `url`, `head_sha`, `state` open, merged or closed as read,
  provenance `orchestrator`), and the event `task.pr_reconciled` with the actor,
  reason, repository, number, URL, head, PR state, task state and contract
  revision.
- **Identity.** A PR is bound once, to one head. Repeating the same repository,
  number and head returns the task and writes nothing (no forge read); another
  head for a bound PR is `409 pr_already_bound`. A later push is the
  continuation's business (observations and reconciliation entries,
  [continuation](task-continuation.spec.md) 3.3), not a rebind.
- **Evidence only.** The binding changes no state, attempt, failure count,
  decision or checklist item, and waives no gate. It supplies the PR artifact the
  completion gate (3.4) looks for; `pr_green` and `pr_merged` still pass the
  closure gate (3.5) against the forge, and open checklist items still send a
  completion to review.

What that means per state:

- `failed`, `cancelled`, `succeeded` stay as they are: a binding never resurrects
  a task. A failed task (the `deliverable_missing` case) moves on only through
  the existing operator `retry`; the next attempt's contract carries the bound
  PR (phase `qa` when merged, `resume` when open), so the worker verifies and
  completes instead of implementing again, and its completion meets the
  deliverable gate. This adds no transition out of a terminal state and no
  review decision without a completion: `retry` is already the one way out of
  `failed`, and the acceptance evidence still comes from an attempt.
- `claimed`: the holder's next `complete` sees the artifact.
- `in_review` (including `deliverable_pending`): the bound PR is the newest PR
  artifact, so the accept reads it.
- Other states: recorded; the next claim sees it.

There is no unbind: the binding is a forge-verified fact and the event is the
audit record. A wrong PR cannot pass the checks; a PR that later changes is
reported by observations, and an accept still reads the forge.

## 3.8 Entry points and engines

- **REST**: `deliverable` on `POST /api/tasks`, the task read and `PATCH`; the
  decision answer routes enforce 3.5; `POST /api/tasks/{id}/reconcile-pr` (3.7).
  **MCP**: `create_task` and `edit_task` take it, `get_task` returns it;
  `answer_decision` goes through the same routes; `reconcile_task_pr` calls the
  binding route. **Dashboard**: a select in the new-task form and the edit form,
  and a line in the task facts; the binding is an orchestrator operation and has
  no dashboard control. **Drafts and routines** use `TaskCreate`'s field.
- **Engines**: the gates are server-side and engine independent. The NanoClaw
  channel states the deliverable, and whether merging is allowed, in the trusted
  part of the prompt, so Claude, Codex, Cursor, Copilot, custom and local
  workers all see the same rule. A runtime that does not use the channel is
  still held to the completion gate. The binding (3.7) is server-side too and
  serves every engine's tasks alike.
- **Way back**: the deliverable can be edited (3.6); a refused completion
  leaves the claim open; a rejected review returns the task to ready; a failed
  task with a bound PR is retried (3.7).

# 4. Open Questions

- Should Argus close `pr_merged` tasks itself once the forge shows the merge
  (an in-process loop)? Deferred: it needs persisted forge state
  ([delivery](task-delivery.spec.md) 4) and a policy on who may auto-close.
- `knowledge` evidence is the author's own write during the attempt. Linking a
  page to its task (a `task` source) would be exact, but the worker tools do not
  pass one yet.
- Should kinds carry a default deliverable (`code_review` is a `response`,
  an implementation kind a `pr_open`)?
- `pr_green` on a PR whose repository reports no checks stays unverified
  forever; lowering the deliverable (3.6) is the way out today.

# 5. Constraints

- Single node, SQLite only; one added column.
- The forge is read only outside write transactions, within the delivery
  budgets; the gate uses no new credential path.
- All writes through `Database.write()` with `tx.now`.
- English only.

# 6. Implementation Consequences

- `argus_core.tasks`: `Deliverable`, `TaskCreate`, `Task`, `ClaimedTask`.
- `apps/api/src/argus_api/db/migrations/0068_task_deliverable.sql`.
- `apps/api/src/argus_api/tasks/deliverable.py`: creation rules, completion
  evidence, accept check. `service.create_task`, `check_create`, `complete`,
  `review`, `edit_task` and the attempt snapshot call it;
  `independent_review` opens a decision for forge-gated deliverables.
- `decisions`: the single and batch answer routes read the forge first and
  pass the result to `answer`.
- `apps/api/src/argus_api/tasks/pr_binding.py`: the binding (3.7), its route and
  MCP `reconcile_task_pr`; `argus_core.delivery.PrBinding` is the request.
- `argus_core.task_edit` (editable fields), MCP `create_task`/`edit_task`
  text, regenerated `openapi.json`/`schema.d.ts`, dashboard forms and facts,
  the NanoClaw prompt line, [task delivery](task-delivery.spec.md) and
  [task edit](task-edit.spec.md).
- Tests: creation rules per level (and drafts, routines, edit), the completion
  gate per level, the pending path, the accept gate (met, unmet, unverified,
  batch, independent review), the editable fields; the binding (success and
  audit event, replay, stale head, wrong repository, unreadable forge, terminal
  and pending states, no forge read while the write lock is held).

## Verification boundary

- Verified by tests: rule and gate behaviour with a fake forge transport.
- Not verified: a live forge, and real workers reporting the evidence the gate
  asks for.
