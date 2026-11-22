---
status: implemented
created: 2026-11-01
updated: 2026-11-22
---

# Task finalization

# 1. Purpose and Scope

An agent's execution ending is not the end of the work around it. After the
attempt's `/complete`, `/fail` or `/block`, the NanoClaw channel may still save a
checkpoint of the workspace. Argus showed none of this: a task looked "done" the
moment the agent exited, and a follow-up that failed or died with its worker was
invisible, so a missing checkpoint was discovered only when the next attempt
blocked. This implements section 3 of
[task operations](../drafts/2026-10-29.task-operations.draft.md): show which step an
attempt is in, record how its follow-up ended, and never read an agent exit as
proof of preservation or delivery.

In scope: the per-attempt record of when the claim ended and of the checkpoint
step; the derived `finalization` on the single-task read (`GET /api/tasks/{id}`,
MCP `get_task`, the dashboard task detail); the fenced worker report of the
checkpoint step; and the audited admin attestation of `nothing_to_save` for one
ended attempt whose worker never reported one.

# 2. Non-Goals

- No change to task states, retry classes, `failures` or attempt counting. The
  outcome of an attempt is whatever `/complete`, `/fail`, `/block` or an expiry
  decided; the follow-up never changes it, in either direction.
- No new task state and no waiting for the follow-up: it is not a gate.
- No delivery step and no workspace step (3.1).
- No duration that the timestamps cannot support (3.4).
- No retry or cancel of a follow-up from here (the admin attestation in 3.3.1
  only recovers an ended attempt that never recorded a checkpoint state).
- No new service; two writes per follow-up; an admin attestation writes one
  audit event (`task.checkpoint_attested`).

# 3. Decisions Made

## 3.1 Findings: which steps exist

Audit of [worker lifecycle](worker-lifecycle.spec.md), [checkpoint](task-checkpoint.spec.md),
[delivery](task-delivery.spec.md), [workspace](task-workspace.spec.md) and the
channel's claim loop (`integrations/nanoclaw/argus.ts`):

- **Execution** is the claim: the session runs under a lease until the channel
  posts `/complete`, `/fail` or `/block`, or Argus ends the claim (expiry, release,
  cancel). Argus changes the task state in that same call.
- **Checkpoint** is the only follow-up step after the agent. The claim loop runs
  `runTask`, then in `finally` closes the session, relays input receipts and calls
  `checkpointAfterAttempt`. It runs after every prepared `repository:write`
  attempt, including completed attempts and hosts without `ARGUS_CHECKPOINT_DIR`.
  When `ARGUS_CHECKPOINT_DIR` is set and Argus did not accept a completion, it
  saves a checkpoint (and supersedes covered checkpoints when clean with a forge
  proof). When the attempt completed or no checkpoint store is configured, it
  writes no checkpoint bundle and instead inspects the workspace read-only to
  prove whether unpublished work exists: it reports `nothing_to_save` only when
  the workspace passes identity and inert git config checks, holds no commits on
  the task branch, detached HEAD or any other local ref missing from
  remote-tracking refs, and has no staged, unstaged or untracked changes.
  Otherwise it reports `failed` (`env_checkpoint_store_missing` when no store is
  configured, `env_checkpoint_unpublished_work` when a completed attempt left
  unpublished work, or `env_checkpoint_invalid` when the workspace cannot be
  verified). Unprepared or workspace-less attempts report nothing, so a blocked
  claim never emits a false `nothing_to_save`. The step is best effort and never
  changes the outcome (checkpoint 3.3). Every other task, engine or runtime has
  no checkpoint step; the channel is the only code that has one.
- **Delivery** is not a step after exit. The agent pushes and opens the PR during
  execution; Argus only reads the forge on request, and merged is not accepted
  (delivery 2, 3.4). It has no start or end that Argus could time, so it is not
  part of this record.
- **Workspace** setup runs before the model starts and is blocked by `env_*`
  reasons. Cleanup and archive are operator actions on ended work
  ([workspace cleanup](workspace-cleanup.spec.md)), unrelated to the attempt's end.
- **Critical path:** none of the follow-up is. The task is already `succeeded`,
  `failed`, `blocked` or requeued when it starts. A retry may be claimed while it
  runs; on the same node the checkpoint lease makes that retry's restore wait as
  `env_checkpoint_busy`, which spends no attempt (checkpoint 3.5).
- **Worker disappears during the follow-up:** the checkpoint may be missing or
  partial-and-ignored (no manifest). Nothing told Argus before; the next attempt
  found out. A worker that dies before the follow-up starts leaves no trace, and
  Argus cannot tell that from "no follow-up applies".
- **Fencing:** every write of an attempt already names `(task, attempt)` and the
  holder (`_owned`: current attempt, assignee, unexpired lease). The follow-up runs
  after the claim has ended, so it cannot use that check. It is fenced by the
  attempt's own row instead (3.3): it can only touch the attempt it names and only
  as the agent that held it, and it writes no column of `tasks`, so a late report
  cannot affect a newer attempt or the task state.

## 3.2 What is recorded

Columns on `task_attempts` (migration `0067`), all nullable:

| Column | Meaning |
| --- | --- |
| `claim_ended_at`, `claim_end_action` | `tx.now` and the action (`complete`, `fail`, `block`, `expire`, `release`, `cancel`, ...) that took the task out of `claimed`, written where the claim ends |
| `checkpoint_state` | `running`, `saved`, `nothing_to_save` or `failed`; null: nothing reported |
| `checkpoint_started_at`, `checkpoint_ended_at` | `tx.now` of the report that set `running`, and of the terminal report |
| `checkpoint_detail` | for `saved` the `checkpoint:` reference, for `failed` the `env_checkpoint_*` reason, for an admin attestation the actor and reason; at most 200 characters, never a path or credential |

Rows from before the migration have none of these and read as no finalization.

## 3.3 Worker report and fencing

`POST /api/tasks/{id}/attempts/{attempt}/checkpoint`, agent credentials only, body
`{state, detail?}` with `state` in `running | saved | nothing_to_save | failed`.
The channel sends `running` before it saves and the outcome after.

- The latest `task_attempts` row of that attempt must exist (404) and belong to the
  caller's agent id (409 `not_attempt_holder`). The task's current state, attempt
  and lease are deliberately not checked, but no `tasks` column is written.
- The claim must have ended (409 `claim_open`): a follow-up cannot start while the
  attempt still runs.
- `running` is accepted once, and a repeat is a no-op. A terminal state is accepted
  only after `running`, and only once; the same terminal state again is a no-op, a
  different one is 409 `checkpoint_closed`. Reports never move backwards.
- A report for attempt N never reads or writes attempt N+1; a worker that lost the
  attempt to someone else gets 409.
- A claim the fence refused before any workspace existed reports `running` then
  `nothing_to_save` after its block, naming the refusal ([task
  workspace](task-workspace.spec.md) 3.7). A claim that finds an earlier attempt's report
  not landed yet reads `GET /api/tasks/{id}/prior-workspaces?attempt=N` (the claim's
  `prior_workspaces` rows, read again) before its fence may block.

## 3.3.1 Admin attestation for legacy unreported attempts

`POST /api/tasks/{id}/attempts/{attempt}/checkpoint/attest`, admin credentials
only, body `{reason, owner_agent_id?, override_failed?}` (required non-blank
`reason`, optional `owner_agent_id` to disambiguate across retry generations,
`override_failed` default false). Recovers a single
ended attempt whose worker exited without recording a checkpoint state (for
example, attempts completed before the channel proved `nothing_to_save` whose
instance has since been replaced):

- Refuses non-admin callers (403), blank reasons (422) and unknown tasks or
  attempts (404).
- Refuses while the claim is still open (409 `claim_open`), while the owner
  agent is `live` (409 `owner_live`), or when a `checkpoint_state` is already
  recorded (409 `checkpoint_already_recorded`). The one exception is `failed`
  with `override_failed: true`, for a failure the admin has since shown to be
  false (for example, the "unpublished" commit is on the forge); `saved`,
  `running` and `nothing_to_save` are never replaced.
- Sets `checkpoint_state` to `nothing_to_save` on that single `task_attempts`
  row (`checkpoint_started_at = checkpoint_ended_at = tx.now`,
  `checkpoint_detail` recording actor and reason, truncated to 200 characters),
  writes no `tasks` column, and appends a `task.checkpoint_attested` audit event
  with actor, attempt, attempt id, owner agent id, owner status, reason and the
  previous state and detail (null when none) in the same transaction. The MCP tool
  takes the same `override_failed` flag.

## 3.4 What the task read shows

`finalization` on `TaskRead` describes the task's newest attempt, or is null when
there is nothing recorded (a pre-migration attempt, no attempt, or a claim that
ended with no row to read). Fields:

- `attempt`, `step`: `executing` (the claim is open), `finalizing_checkpoint`
  (follow-up reported `running` and recently), `finished` (the claim ended and no
  follow-up is pending) or `unknown` (follow-up reported `running` and then went
  silent for `CHECKPOINT_STALE_S` = 600 s: its worker may have died; the state is
  never inferred to be success or failure).
- `claim_ended_at`, `claim_end_action`.
- `execution_s`: `claim_ended_at - claimed_at`, **only** when the claim ended
  through `complete`, `fail` or `block`. For expiry, release or cancel Argus ended
  the claim and the agent may have stopped earlier or later, so no duration is
  given.
- `checkpoint`: null when nothing was reported (no checkpoint applies, is
  configured, or the worker died before starting; "no follow-up" is never shown as
  "saved"), else `{state, started_at, ended_at, duration_s, detail}` where `state`
  also includes `unknown` (derived as above) and `duration_s` exists only when both
  timestamps do. Both are Argus clock reads of two worker reports, so the duration
  includes the worker's network latency and is exact to seconds, which is all it
  claims.

`finished` with `checkpoint` null or `failed` says nothing about preservation. The
agent's exit, a succeeded task or a clean `/complete` never implies a saved
checkpoint, and the delivery state keeps its own projection.

Only the single-task read carries it; lists do not.

## 3.5 Dashboard

The task detail Activity panel shows one line for the newest attempt: "Agent
running", "Saving checkpoint…", "Agent finished (took N)" and, when reported, the
checkpoint outcome ("Checkpoint saved (took N)", "Nothing to save", "Checkpoint
failed: <reason>", "Checkpoint state unknown: worker silent since <time>"). No
sentence is shown for a null `finalization`, and none claims delivery.

## 3.6 Entry points and engines

- REST: the worker report (`POST /api/tasks/{id}/attempts/{attempt}/checkpoint`),
  the admin attestation (`POST /api/tasks/{id}/attempts/{attempt}/checkpoint/attest`),
  and the single-task read. MCP: `get_task` returns the read unchanged;
  `attest_checkpoint_nothing_to_save` (admin-only scope, hidden from workers and
  non-admin agent tokens) forwards to the admin attestation route. The dashboard
  reads only.
- Engines: the step belongs to the NanoClaw channel, so Claude, Codex, Cursor,
  Copilot, custom and local tasks run through it get it equally; a runtime that
  does not use the channel reports nothing and shows `checkpoint: null`.
- Way back: a report or attestation cannot be undone; a later attempt carries its
  own record.

# 4. Open Questions

- Should a failed or unknown checkpoint raise an inbox row, or is the task detail
  enough? Today it only informs.
- Workspace cleanup and archive could join the record once they run per attempt.

# 5. Constraints

- Single node, SQLite only; the report is one `Database.write()` using `tx.now`,
  with no network call inside the transaction.
- Migration `0067` only adds nullable columns; no shipped migration changes.
- The report never changes `tasks`, `failures` or any retry decision.
- English only; no paths or credentials in `detail`.

# 6. Implementation Consequences

- `apps/api/src/argus_api/db/migrations/0067_task_finalization.sql`.
- `apps/api/src/argus_api/tasks/finalization.py`: record, report, admin
  attestation and derive; `_apply` records the claim end; `views.read_task` adds
  `finalization`; routes in `tasks/routes.py`; MCP tool
  `attest_checkpoint_nothing_to_save` in `mcp_server.py`; models in
  `packages/core`; `make openapi`.
- `integrations/nanoclaw/argus-workspace.ts` and `argus.ts`:
  `checkWorkspaceClean` proves a prepared workspace holds no unpublished commits
  or uncommitted changes; `checkpointAfterAttempt` reports `running` and the
  outcome after every prepared `repository:write` attempt, including completed
  attempts and hosts without a checkpoint store (best effort; a failed report is
  logged and changes nothing).
- Dashboard Activity panel and its test.
- Tests: `apps/api/tests/test_task_finalization.py` (fencing, ordering,
  idempotence, admin attestation and audit, derivation, durations, migration);
  `apps/api/tests/test_mcp.py` (admin MCP attestation and scope); channel tests
  in `argus-workspace.test.ts` and `argus-accounts.test.ts`; the dashboard test.

## Verification boundary

- Verified by tests: transitions and fencing of the report, admin attestation
  and its audit event and refusals (open claim, live owner, recorded state,
  non-admin caller), MCP admin tool scoping, stale `running` derived `unknown`,
  durations only where both timestamps exist, claim end recorded for every
  ending action, REST read, channel reports with and without a checkpoint store
  and on completed attempts, dashboard copy.
- Not verified: a real worker dying mid-checkpoint on a live node; a retry racing
  a running checkpoint across two live workers.
