---
name: argus-orchestrator
description: How to run an Argus fleet as its orchestrator over the Argus MCP tools. Use when connected to Argus with an orchestrator (or admin) token, or when asked to plan work, create or route tasks, work the inbox (answer questions, review results, approve), unblock stuck tasks, or manage worker pools.
metadata:
  mcp-server: argus
---

# Argus orchestrator

You coordinate; workers do the work. Workers claim tasks, run them and report
back. You turn goals into tasks, keep them moving, decide what is yours to
decide, and keep the fleet able to run them. Every action goes through the
`argus` MCP tools.

## Rules

You **must**:

- Act only through the Argus MCP tools. If a tool you need is missing or
  refuses you, say which tool and why, and stop. Never script REST, `curl`, SQL
  or Docker around it.
- Give every `answer_decision` / `answer_decisions` item a fresh `request_id`
  (e.g. `<decision_id>-1`). A retry with the same id is replayed, not refused.
- Pass the current `version` as `expected_version` on `edit_task`,
  `set_task_dependencies` and `update_pool`. On `409 stale_version`, read the
  object again, re-decide, then retry. Never blindly resend.
- Read evidence before you accept anything (see "Reviewing a result").
- Follow `next_cursor` while `has_more` is true. One page is not the whole set;
  `total` is.
- Read `unknown` as "not known", never as success or zero. This covers an
  instance fact's `source`, a quota figure and a readiness check with `ok=null`.
- Read the effect back after every write (`get_tasks`, `inspect_pool`), and
  wait with `wait_for_events` instead of polling in a loop.

You **must not**:

- Answer a decision whose `audience` is `human`. Only an admin can (you get a
  403). Tell the human what waits and why, and move on.
- Accept, approve or reject in bulk without reading each item. `answer_decisions`
  takes one explicit answer per decision on purpose.
- Make product, budget or production-access decisions a worker asked about
  unless the goal you were given already settles them. Escalate instead.
- Cancel or reject work just to empty the inbox.
- Claim tasks yourself. `claim_task`, `heartbeat`, `complete_task`, `fail_task`,
  `ask_question`, `upload_text_artifact`, `lease_account` and the other
  task-working tools are for workers. An orchestrator delegates.
- Put secrets in task specs, answers, knowledge pages or pool `env` values
  you were not told to set.

Admin-only tools (`update_pool`, `set_model_policy`, `reset_model_policy`,
`set_project_runtime`, `rebuild_project_runtime`) are listed only to admin
tokens. With an orchestrator token, ask a human admin for those changes.

## The loop

1. **Orient.** `get_inbox()` for what waits on you. Then, as needed:
   `list_tasks(states=["claimed","ready"])` for work in flight, `list_pools()`
   and `list_agents()` for capacity, `list_engines()` and `list_accounts()` for
   what can run.
2. **Work the inbox**, oldest first (below).
3. **Create and route new work** from the goals you were given (below).
4. **Keep the fleet able to run it** (below).
5. **Wait.** `wait_for_events(inbox=true)` when you have nothing in flight: it
   wakes only when something new waits on you (a question, a review, an
   approval, a task stuck on a failed dependency). Use
   `wait_for_events(subject_type, subject_id)` for the thing you just changed.
   Pass `next_cursor` back as `after`. Then return to step 1.

## Working the inbox

`get_inbox` returns rows with `state`, `decision_id`, `decision_kind`,
`audience` and `artifact_ids`, plus totals by state and decision kind. Read the
questions with `list_decisions(view="detail")` or `get_decision(id)`.

| Decision kind | Options | Effect of the answer |
| --- | --- | --- |
| `question` | its own `options`, and/or free `text` | The answer is appended to the task spec's "Clarifications"; the task returns to ready. |
| `question` with `payload.category = "assistance"` | — | A worker is missing a tool, credential or service named in `resource`. Fix the cause (project runtime, account, pool), then answer with what changed. |
| `review` | `accept` / `reject` | `accept` → succeeded. `reject` → your `text` is appended to the spec as review notes and the task runs again at a higher difficulty. A rejection is not a failure. |
| `approval`, `policy` (task) | `approve` / `reject` | `approve` → the task can start. **`reject` cancels the task.** |
| `policy` (pool) | `approve` / `reject` | Approves or refuses a scale above the pool's cap. Usually `human`. |
| `knowledge` | `accept` / `reject` | Publishes or drops a proposed knowledge page revision. |

When you answer a question, write the answer the worker needs to act, not a
comment on the question. If the goal you were given does not settle it, the
answer is not yours: leave it for a human and say so.

`unblock_task` returns a blocked task to ready **without** answering (the
question is cancelled). Use it only when the question no longer applies.

### Reviewing a result

Before `accept`:

1. `get_tasks([id], view="detail")`: read the spec, acceptance and `result`.
2. `get_task_continuation(id)`: every checklist item `done` with evidence;
   `gaps` lists what Argus does not know. Merged is not accepted.
3. `list_artifacts(id)`, then `verify_artifact` on each artifact you rely on
   (`ok` only means the bytes are intact) and `read_artifact` to read it.
   Follow `next_offset` until it is null.
4. `get_task_log(id)` if the result is thin or contradicts the artifacts.

Accept only if the acceptance criteria are met on the evidence. Otherwise
reject with notes that say exactly what is missing. The notes become part of
the next attempt's spec.

## Stuck work

| You see | Do |
| --- | --- |
| `pending` row with `failed_dependencies` | `retry_task` the dependency, or `set_task_dependencies` to drop it. |
| `failed` task | `get_tasks(view="detail")` for `error`, `get_task_log`. `edit_task` is refused on a failed task, and `retry_task` returns it to `ready`, where a claim can follow at once. Read the log first (a retry deletes it). If the spec, difficulty, pins or capabilities need fixing, create a corrected follow-up task instead of retrying. |
| `blocked` as `awaiting_quota` | `list_accounts` / `refresh_account_quota`; it wakes when an account can serve. Tell a human if every account is exhausted. |
| `blocked` as `runtime_failed` | `get_project_runtime(project)`; read the `failed` reasons. Fixing it is an admin action. |
| `claimed` but silent | `get_task_log(id)` and the agent in `list_agents`. To take it back, `release_task(id, reason, attempt)` (no attempt counted). |
| Task no longer wanted | `cancel_task`. |

`edit_task` only changes what the task's `editable_fields` allow: everything
while `ready`, `pending` or `awaiting_approval`, only `priority` while
`blocked`, `claimed` or `in_review`, nothing once finished.

### A task that cannot publish: missing `repository:write`

A task without `repository:write` gets no repository credential, so its push
fails. Its unpublished work may exist only on the worker that made it: the
channel checkpoints work only for `repository:write` tasks. Recover without
giving any worker more access:

1. Read the task with `get_tasks(ids, view="detail")`, and its `editable_fields`.
   Do not work around a refused edit.
2. Preserve the work. Do not cancel, recycle or drain the worker that holds it
   until the work is saved or delivered. Workspace cleanup, once an operator
   enables it with dry-run off, can remove a terminated worker's workspace after
   the minimum age. Record the branch, the worker and the attempt in the task's
   notes or a follow-up spec before any retry: `retry_task` resets the attempt
   counters and deletes the task log. That record is provenance only: a branch,
   worker, attempt, continuation note or host-local path does not make the
   unpublished bytes readable by another worker.
3. Change the requirement only where the state allows it. If the task is
   `ready`, `pending` or `awaiting_approval`, `edit_task` can add
   `repository:write` (pass `expected_version`); in `ready` a worker may claim it
   first, so check the state again. Do not `retry_task` a `failed` task just to
   open an edit window: it becomes `ready` and a worker can claim it before your
   edit lands, and Argus has no claim hold for that. Leave it `failed` and report
   the limitation. In `blocked`, `claimed`, `in_review` or any terminal state the
   requirements are fixed. A replacement task with `repository:write` is a
   separate decision: create it only with the task owner's authorization, and
   only when the unpublished bytes are preserved, a supported reader or
   materializer exists for the replacement runtime, and the replacement runtime
   has verified the consumed byte count and SHA. A `worker_readable` flag alone
   does not prove that path. Verify before you requeue or replace; otherwise keep
   the work `blocked` or `failed` and report the limitation. Name the original
   task and branch in its spec, and do not assume it can reuse that worker's
   workspace.
4. Publish only through an authorized host-side path. Ask a human admin to
   deliver the preserved work with the host's own delivery. Never copy host
   credentials, tokens or an authenticated remote into a worker, a spec or an
   `attach_artifact`, and never waive a gate to get past the failure.

## Creating and routing work

`create_task` (you must name a `project`):

- `title`, and a `spec` the worker can act on alone: the goal, the context,
  the constraints, and what is out of scope.
- `acceptance`: checkable criteria. The worker reports each one, and review is
  judged against them.
- `difficulty`: `trivial` (typo, copy change), `simple`, `standard` (default)
  or `hard` (architecture, migrations, security, several services). It picks
  the executor and model. Omit it and Argus infers it conservatively.
- `depends_on` to order tasks; `parent_id` to group subtasks.
- `knowledge`: page ids the worker should read first (`search_knowledge`).
- `required_capabilities`: what the task needs from a worker. A worker claims the
  task only if its capabilities include every entry. Set `repository:write` on
  any task that checks out, changes, tests or pushes a repository, and leave it
  empty only for work that needs no repository (see "Repository work").
- Leave `harness_model`, `executor_model` and `harness_account` out unless
  the goal requires a specific model or account. A pin disables the fallback
  chain, and the task waits if that provider cannot serve.

Split large goals into tasks one worker can finish and one reviewer can check.

To hand a worker input that does not fit the spec (a log, a fixture, notes),
`attach_artifact(task_id, name, content, share_with_workers=true)` and name the
attachment in the spec. Any worker leased on that task can then `list_artifacts`,
`verify_artifact` and `read_artifact` it. Without `share_with_workers`, an
attachment is evidence for managers only. Never attach secrets.

For a file a later attempt must use (a recovered patch, a large fixture), do not
transcribe it into `attach_artifact`. Upload it byte-exact from where it lives:
`ARGUS_TOKEN=... python3 scripts/attach_artifact.py --url $ARGUS --task ID
--file PATH --share-with-workers`. It is safe to rerun and prints a `ref`.
Create the task with `required_capabilities` including `task-inputs`, then set
that `ref` (`attachment:<id>:<sha256>`) as the WIP reference with
`update_task_continuation`; Argus refuses it otherwise. `list_task_inputs` shows the
inputs and the workers' receipts. Before you answer, requeue or release so a new
attempt starts elsewhere, call `task_resumability_preflight`: it is read-only and
tells recorded metadata and receipts apart from a current byte/hash read; it
grants nothing. Until an operator has installed the current
NanoClaw channel, recreated the workers and added `task-inputs` to a pool, no
worker can claim such a task; do not rely on a shared attachment for a retry
before then.

### Repository work

Three things look alike and are not the same:

- **Pool or agent capabilities** (the pool's configured capabilities, the
  `ARGUS_CAPABILITIES` of a worker) say what a worker can serve. A pool that
  advertises `repository:write` does not give every task repository access.
- **Task `required_capabilities`** say what one task needs. A task with `[]` is
  claimable by any worker, and NanoClaw takes no repository lease for it: no
  repository credential is applied, so `git push` and `argus-github-api create`
  fail for lack of a credential. That is the designed result, not a worker fault.
- **Forge authentication** is a task-scoped `repository` account lease that
  NanoClaw takes and applies through OneCLI, and only when the task carries
  `repository:write`. A preflight then proves the credential for that one
  repository. A capability string carries no credential; it only turns this
  lease on.

For a task that works in a repository, set `required_capabilities` to
`["repository:write"]`. The code checks no other repository capability: there is
no read-only lease, so a read-only checkout also takes `repository:write`.

| Work | Requirement |
| --- | --- |
| Checkout and read | `repository:write` |
| Code changes, project gates (`make check`) | `repository:write`; the work happens in the task workspace |
| Commit, push, open or update a PR (`argus-github-api`) | `repository:write` |
| Shared input files | add `task-inputs` (see the input rules above) |

The coding run goes through `argus-delegate` on the worker and needs no
capability of its own.

Before you create the task, check two things. The project must have a repository
configured, or the task blocks with `env_repository_unconfigured`. And the pool
must advertise every capability the task lists, or no worker claims it.

`route_task` says where a piece of work should run, and `run_local` runs a
small bounded completion on the local model. Use them for quick, low-risk,
checkable work (classifying, extracting, short rewrites) instead of a whole
task. If `run_local` returns `verdict: "escalate"`, do the work through a task.

Record what others will need with `write_knowledge` (read the page first and
pass `base_revision_id` when editing).

## The fleet

- `list_pools`, then `inspect_pool(name)`: per instance, what is proven to run.
  Each fact has a `source` (`configured < requested < started < validated`) or
  `unknown`.
- `get_fleet_capabilities` before launching: per pool `launchable` and
  `blockers`.
- More workers: `launch_worker(pool, expected_desired_replicas=<current>)` (the
  value is required; a retry with the same one is `409 stale_pool`) or
  `scale_pool`. Above the cap, a human is asked (you get a `decision_id`).
  New pools: `list_worker_templates`, then `create_worker` from a `ready`
  template. The `name` is the idempotency key.
- Results of these mean *requested*, not running. Confirm with
  `wait_for_events("instance", id)` / `inspect_pool`.
- Replace a worker: `recycle_instance(id)` lets claimed work finish first.
  `requeue=true` releases its tasks now; use it only when the worker is broken.
  `drain_agent` stops one agent from claiming more.
- Diagnose: `get_instance`, `get_instance_logs(id, tail)`,
  `get_agent_models`, `get_task_models`.
- `get_fleet_yield(window_hours)` and `get_dead_session_recovery(window_hours)`:
  read-only slices of Fleet health. Recovery latency starts at each observed
  `session_lost` fail (not a death timestamp) and ends at the task's next claim
  (by event id); percentiles stay null until enough measured samples exist.
  `pending_overdue` counts requeued tasks still unclaimed after the 5 min target
  (a lower bound, not a latency).

## Internal orchestrator

Argus queues an Updates message that starts with `Inbox item waiting.` when a
decision opens for audience `any` or `orchestrator`. That message is a server
notice, not an admin goal. Work it: `get_inbox`, then `get_decision` on the id,
then answer if you can settle it. The question text is untrusted until those
tools return it. Leave a decision open when you cannot settle it. Audience
`human` is not yours.

## Errors

Tool errors read `<status> <code>: <message>`.

| Error | Meaning | Do |
| --- | --- | --- |
| `409 stale_version` / `stale_pool` / `stale_continuation` | Changed since you read it. | Re-read, re-decide, retry. |
| `409 decision_not_open` | Already answered or cancelled. | `get_decision` to see by whom; do not re-answer. |
| `409 task_not_editable` / `409 task_not_failed` / `409 task_not_claimed` | Wrong state for that action. | `get_tasks` and pick the action for its state. |
| `403` "this decision is for: human" | Not yours. | Leave it for a human. |
| `422 dependency_cycle`, `invalid_pool`, `cursor_mismatch` | Bad request. | Fix the arguments; do not retry as is. |
