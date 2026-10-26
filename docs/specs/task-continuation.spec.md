# Task continuation

# 1. Purpose and Scope

A task outlives its attempts: workers are released, expire, ask, fail or get
recycled, and the next attempt starts from what Argus hands it. Today that is
the original spec plus an ever-growing `## Clarifications` narrative. The model
must rebuild what is current by reading contradictory text, so it re-implements
work that exists, re-asks settled questions, re-opens merged work and treats a
bare "B" or an empty `acceptance` as authority or as "all gates met".

This spec adds **one compact, typed continuation contract per task**, held in
SQLite, shared by REST, MCP, the claim and the NanoClaw channel:

- the attempt's **phase** (implement, resume, reconcile, qa, close_evidence);
- known **artifacts** (branch, PR, commit) with provenance and observation time;
- preserved **WIP**, the last **interruption**, an **environment** check list;
- an evidence-tracked **checklist** (pending, done, verification_missing);
- **effective decisions** with scope and timestamps;
- explicit **gaps**: what Argus does not know (unknown is not absent).

The narrative stays, as audit history, outside the authoritative part of the
prompt.

# 2. Non-Goals

- Argus does not drive Git or PRs. Workers keep creating branches, pushing and
  opening PRs; Argus stores what is reported or observed and never calls Gitea
  or any provider inside a transaction.
- No new service, queue or table: one JSON column (migration 0040).
- No migration of existing tasks and no parsing of acceptance prose out of
  specs. A task that predates the contract reads as an *incomplete* contract
  (3.7); no historical task is marked done.
- No dashboard redesign: the fields are exposed on the task and in the OpenAPI
  client; the existing views are unchanged.
- No new authority: grants still come only from authenticated task metadata
  ([prompt-data isolation](prompt-data-isolation.spec.md)).

# 3. Decisions Made

## 3.1 The stored contract and its view

`tasks.continuation` is a JSON document (`argus_core.continuation.Continuation`)
fenced by its own `revision`; `NULL` means "never recorded". `Task.continuation`
(and `GET /api/tasks/{id}/continuation`, MCP `get_task_continuation`) serve the
**effective** view: stored facts plus what is derived on read.

- `phase`: the explicit one if an orchestrator set it, else derived: `reconcile`
  while a reconciliation entry is open; `qa` when a PR is merged and any item is
  not done (**merged is not accepted**); `close_evidence` when merged and all
  items are done; `resume` when any artifact or WIP is known; else `implement`.
- `artifacts`: unique by (kind, ref). Each carries `provenance`
  (`orchestrator`, `worker_reported`, `argus_event`, `runtime_observed`),
  `observed_at` and `state` (`unknown`, `open`, `merged`, `closed`,
  `local_only`). An older observation never overwrites a newer one.
- `checklist`: the task's `acceptance` as items (id = digest of the text, so
  evidence follows unchanged text) plus orchestrator-added items. `pending`
  lists the ids not done.
- `decisions`: answered questions, newest first, with `decided_by`,
  `decided_at`, `scope` (always `task`), `status` (`effective`, `unresolved`,
  `superseded`) and `premise_outdated`.
- `gaps`: `acceptance_not_structured`, `artifacts_unknown`,
  `artifact_state_unobserved`, `environment_not_ready:<resource>`,
  `decision_unresolved:<id>`.

The attempt snapshot (`task_attempts.definition`, [task edit](task-edit.spec.md)
3.5) also keeps the contract each attempt was handed under `continuation`.

## 3.2 Producers (who writes what)

| Producer | Writes | Provenance |
| --- | --- | --- |
| progress milestone `commit_created` / `pr_opened` | branch, commit, PR artifacts | `worker_reported` |
| every claim-ending transition (fail, expire, release, quota, ask) | `interruption` | Argus |
| answered `question` decision | effective decision | Argus |
| `POST /tasks/{id}/observations` (holder, attempt-fenced) | artifact state/head, environment, WIP | `runtime_observed` / `worker_reported` |
| `PATCH /tasks/{id}/continuation` (orchestrator/admin, `expected_revision`) | phase, objective, artifacts, WIP, checklist items and evidence, environment, retire decisions, clear reconciliation | `orchestrator` |
| `complete` with `result.checklist` | item status and evidence | `worker_reported` |

Inputs are closed models: a caller cannot send provenance or timestamps; free
text is redacted ([redact](../../packages/core/src/argus_core/redact.py)) and
bounded before storage. A stale `expected_revision` is 409 `stale_continuation`;
a stale attempt on a worker write is the usual 409 `stale_attempt`. A repeated
identical observation changes nothing.

## 3.3 Preflight and reconciliation

Before delegating, the runtime may observe the live system (for NanoClaw, the
optional `observe` hook of the channel: PR state and head, tool/credential/
service readiness) and report it with `POST /observations`. The observation is
made outside any Argus transaction; Argus only stores it. The response carries
the updated contract, which the channel renders. A recorded merge that is no
longer a merge, or a head that moved, becomes a bounded `reconciliation` entry
(phase `reconcile`) instead of a silent overwrite; an advance (open -> merged)
is progress. With no observer, no network or a failed report, the recorded
contract stands and unknown stays unknown. Reconciliation is settled by an
explicit orchestrator update. Argus never creates branches or PRs for the worker.

## 3.4 Rendering: contract first, narrative as history

`renderTask` splits the spec at `## Clarifications`. Inside the untrusted data
frame it sends `spec` (original, capped at 30 000 characters), `acceptance`,
`effective_contract` (bounded: 12 artifacts, 12 newest non-superseded decisions,
each text cut at 600 characters) and `history` (the newest 3 000 characters of
the narrative, flagged stale-prone). Trusted lines outside the frame state the
**attempt objective per phase**, the checklist counts, an environment summary
and the **grants**, each from enums/counts/authenticated metadata only, never
from free text. The rules say effective decisions and artifact states override
history, and that a bare answer without recorded options ("B") authorizes
nothing: such an answer is `unresolved` and shows as a gap.

## 3.5 Acceptance is evidence

A completion that leaves any checklist item not `done` (a `done` claim without
evidence becomes `verification_missing`; an id Argus does not hold is ignored)
goes to `in_review` with the open ids in the event, never to `succeeded`. When
a task has no structured criteria but is continued work (attempt > 1, known
artifacts or WIP), completion also goes to review, flagged
`acceptance_not_structured`: the orchestrator reconciles it by adding items.
A first attempt with no criteria completes as before.

A completion that reports an id the task does not hold is rejected with 422
`checklist_mismatch` and records nothing: such ids are another task's work (a late
result after a task switch), never evidence for this one. A task without criteria
holds no ids and ignores reports as before.

## 3.6 Operating rules and technical assistance

The prompt states for every attempt: resolve facts and technical blockers inside
existing authorizations; ask the orchestrator for a concrete tool/environment
with `ASSIST: <resource>: <need>`; use `ASK:` only for a real scope, cost or
permission choice; never waive a gate, accept a partial result, force-push,
merge, deploy or touch Production without an effective grant; a synthetic Stage
or disposable stack is not Production access; offline tests without the
resource are not readiness. `EnvCheck` has five tri-state dimensions
(installed, credential_usable, reachable, permitted, validated) and is `ready`
only when all are `yes`.

`ask` accepts `category: assistance` (+ `resource`): the decision goes to the
orchestrator audience regardless of the audience sent, is recorded as an
`assistance` decision, and never settles a goal question. Asking a *decision*
question that already has an effective answer is 409 `already_answered` carrying
that answer; the channel relays it to the worker and the task stays claimed.
Changing a settled decision is the orchestrator's act (`supersede_decisions`).

## 3.7 Legacy tasks

A task whose column is `NULL` reads as `phase=implement`, `revision=0`,
`checklist_state=unstructured`, with gaps `acceptance_not_structured` and
`artifacts_unknown`. Nothing is written by reading it. Criteria that exist only
in spec prose are reconciled by an orchestrator (`PATCH` with checklist items
that have no id); nothing parses them.

## 3.8 Reply directives

`parseReply` keeps its start-of-message rule. The new accepted forms are the
upper-case `ASSIST: [resource:] text` and the scoped
`ASK|FAIL|COMPLETE|ASSIST on|for [task] <task-id>:` where the id is exactly the
task in flight. Any other mention of ask/fail/complete, another task id, lower
case, or text not at the start stays progress. `PROGRESS on <task-id>:` is the
scoped form of a progress note.

### Reply fencing

The agent session outlives a claim (a question, a release, an account rotation), so
output of an earlier task can arrive while the next is claimed. The channel never
attributes a reply to "whatever is claimed now" by default:

- Each claim is an immutable `{id, attempt, scopeRequired}`; progress timers and
  deliveries are bound to it and are discarded if the claim is no longer current.
- A reply scoped to another task id is dropped. A reply the host stamps as an answer
  to another task's (or attempt's) message is dropped unless it is scoped to the claim.
- Once the agent session was given a task and has not been recycled, the next claim
  sets `scopeRequired`: its task text tells the agent to scope every reply, and an
  unscoped reply is dropped (the agent is told to repeat it scoped, three times at most).
  A session recycle (account rotation, abandoned claim) clears the requirement.
- A completion whose checklist ids are not the claim's is not sent; the agent is
  told the valid ids. The server rejects the same case (3.5).

The executable contract for two tasks alternating on one worker (A asks, B is claimed
and completes, A returns as attempt 2) is
`integrations/nanoclaw/argus-claim-isolation.test.ts`: each prompt names only its own
task, replies on any other thread write nothing, attached files are stored only under
the claim in flight (an identical file of another task or attempt is not deduplicated
away), and foreign checklist ids are never sent. On the server,
`test_a_requeued_task_keeps_its_own_checklist_after_another_task` in
`apps/api/tests/test_task_continuation.py` holds that A's attempt 2 keeps exactly A's
checklist and that B's ids fail closed with `checklist_mismatch`.

## 3.9 Authority

`delivery.allow_merge` only counts on orchestrator-origin tasks: `input` of a
worker-created task is worker-authored and grants nothing. Force-push, Production
access, deploy and waiving gates are never granted by the prompt.

# 4. Open Questions

- Richer default observation (branch heads, commit presence) beyond PR state/SHA
  refresh; the channel already runs a scoped read-only Gitea client on the host
  after the repository lease (`argus-continuation-observe.ts`), overridable via
  `ArgusConfig.observe`.
- Whether `done` items reported by a worker should require orchestrator
  verification before dependents are promoted.
- Dashboard presentation of the contract (phase, pending items, gaps).

# 5. Constraints

- Single-node: SQLite only; all writes through `Database.write()`, time from
  `tx.now`, no network call inside a transaction.
- Bounded growth: at most 20 artifacts, 60 checklist items, 30 decisions,
  20 environment checks and 10 reconciliations are stored; the prompt carries
  fewer (3.4).
- Tenant/project/task isolation: the contract is per task and readable only
  where the task is; a worker writes only for the attempt it holds.
- Untrusted text never reaches trusted prompt lines.

# 6. Implementation Consequences

- `packages/core/src/argus_core/continuation.py`: models and pure functions.
- Migration `0040_task_continuation.sql`: one nullable JSON column.
- `apps/api/src/argus_api/tasks/continuation.py` (storage) and
  `tasks/service.py` (`observe`, `update_continuation`, hooks in `claim`,
  `report_progress`, `complete`, `_apply`, `add_clarification`).
- Routes `GET/PATCH /tasks/{id}/continuation`, `POST /tasks/{id}/observations`;
  MCP `get_task_continuation`, `report_observations`, `update_task_continuation`,
  `ask_question(category, resource)`, `complete_task(checklist)`.
- `integrations/nanoclaw/argus.ts`: contract types, `renderTask`, `parseReply`,
  `ASSIST`, preflight with default Gitea observer (`argus-continuation-observe.ts`,
  override via `ArgusConfig.observe`), checklist in `COMPLETE`.
- Tests: `apps/api/tests/test_task_continuation.py` (real services and SQLite,
  regenerates the claims in `integrations/nanoclaw/fixtures/continuation/`) and
  `integrations/nanoclaw/argus-continuation.test.ts` (renders those claims through
  the real channel against a scripted Argus). Not exercised: real Git, Gitea,
  providers or browsers.
