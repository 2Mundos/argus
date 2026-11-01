# Quota resume: resume the same native conversation after a quota pause

# 1. Purpose and Scope

[Account selection](account-selection.spec.md) pauses a task on a provider quota
notice without spending an attempt: it goes back to `ready`, or parks as
`awaiting_quota`. The next claim starts a new agent session and the
[task checkpoint](task-checkpoint.spec.md) restores files, but the model starts
over. This spec keeps the native conversation across that pause: the paused attempt
records the Claude Code session id, and the next claim on the same node and engine
hands the agent one framed operator message instead of a cold start. When that is
not possible the claim behaves exactly as before.

The scope is `task_attempts.native_session` (migration `0066`),
`argus_api/tasks/quota_resume.py`, the `native_session` field of the `/fail` and
`/exhausted` reports, `quota_resume` on the claim response, `POST
/tasks/{id}/quota-resume`, and the channel files `argus-quota-resume.ts` and
`argus.ts`.

# 2. Non-Goals

- No resume after `session_lost`, a crash or a stall, and no cross-node or
  cross-engine resume. Those belong to P4 of
  [worker efficiency](../drafts/2026-10-24.worker-efficiency.draft.md).
- No Codex, Cursor, Copilot, custom or local resume (3.6).
- No claim that the credential swap works inside a live session (4).
- No change to the attempt counter, failure counts, the quota patterns or
  `awaiting_quota`.
- No dashboard change, no MCP tool: the data is channel-to-API plumbing (6).

# 3. Decisions Made

## 3.1 What is recorded

`task_attempts.native_session` is JSON `{session_id, engine, node}`;
`native_session_used_by` is the attempt that took it. Both are null for attempts
that ended any other way.

## 3.2 Only a quota pause records it

The channel reports `native_session` (`session_id`: 8-64 characters of
`[A-Za-z0-9_-]`, plus `engine`) on `/fail` and `/exhausted`. Argus keeps it only
when it itself classifies the report as a quota pause: in `fail_task` right after
`pause_for_quota`, and in `exhausted` when no next account exists. A generic
failure, `session_lost`, a stall or a crash keeps nothing, whatever the channel
sent. The first write wins. The node is the caller's agent id, never a body field.

## 3.3 The claim offer

`ClaimedTask.quota_resume` is `{session_id, engine, node, from_attempt}` when the
attempt just before the claimed one has an unused session, otherwise null. Only
the immediately previous attempt is offered. The attempt counter and failure count
are unchanged: `claim` bumps `attempt` and a quota pause never touches
`failures`, as before.

## 3.4 Resume or fall back

The channel decides in this order, and the first match is the fallback reason:
`other_node`, `other_engine`, `engine_unsupported` (only `anthropic` can resume),
`not_available` (no way to start a session from a native id, see 4),
`unreadable` (the transcript file is gone or empty). A fallback is today's fresh
session with the checkpoint restored. A resume is reported only when the session
was actually resumed; a fresh conversation is never reported as one.

## 3.5 One message, once

On a resume the channel posts `POST /tasks/{id}/quota-resume` with the outcome
(`resumed` or `fallback` plus a reason). The call is lease-fenced, marks the
session used and records `task.quota_resume` once; it answers `{first: true}` only
the first time. The channel sends the framed message only on `first: true`. The
message uses the task-steering framing ([task steering](task-steering.spec.md)
`renderSteer`): "Your previous account hit a usage limit and the account was
switched. Continue from where you stopped.", followed by the current task text, as
one inbound message. A retry, a re-claim or a second call never sends again. The
accepted cost: a crash between the take and the send loses the note, never
duplicates it. A fallback also consumes the session.

## 3.6 Engines

| Engine | Decision |
| --- | --- |
| Claude (`anthropic`) | Records and offers. Resume needs 4. |
| Codex | Unsupported. Nothing shows a thread resumes under another ChatGPT account. |
| Cursor, Copilot, custom, local | Unsupported. The channel sends no session. |

## 3.7 Observing the session id

NanoClaw's own session id is not given to the channel (a chat `session_ref` is the
thread). The Claude Code session id is visible on the host as the transcript
filename `<id>.jsonl` under `.claude-shared/projects/<project>/` of the group's
session directory. The channel takes it only when exactly one non-empty transcript
was written since the claim; zero or several mean no session is recorded.

## 3.8 Way back

Not resuming is today's behaviour, so every failure mode degrades to it. There is
nothing to undo: an offered session is used once or ignored.

# 4. Open Questions

- **Starting a NanoClaw session from a native id.** Nothing in this repository
  shows how. `nativeSessions.resume` is therefore not wired in `fromEnv`, and a real
  node always reports `fallback` / `not_available`. Until it is wired and proven,
  no resume happens.
- **Credential swap.** Argus recreates the group's session containers around every
  account rotation and the session files survive
  ([research](../research/2026-10-29.quota-resume.research.md)). Whether the
  resumed session accepts the new account's credentials is unverified.
- **Validity cutoff.** The only check is that the transcript exists and is not
  empty. No age or context-size cutoff is implemented; the provider's retention is
  unknown.
- **Codex with another account.** Unverified, so unsupported (3.6).

# 5. Constraints

- The offer comes from the previous attempt of the same task only; nothing crosses
  tasks.
- Writes use `Database.write()` and `tx.now`; no network call in a transaction.
- The session id is not a secret, but it is recorded only with a node and engine,
  and the report validates its shape.
- The channel adds no new required service.

# 6. Implementation Consequences

- Migration `0066_quota_resume.sql`; API models in `argus_core.accounts` and
  `argus_core.tasks`; `make openapi` regenerates the contract.
- Entry points: REST only. Routines and the orchestrator do not need it; an MCP
  tool would add nothing, because the claim is the only reader.
- Update [account selection](account-selection.spec.md) pointers when the resume is
  wired; the draft is superseded by this spec.

## Verification boundary

- Commands: `uv run pytest apps/api/tests/test_account_failover.py`;
  `integrations/nanoclaw/argus-quota-resume.test.ts` (vitest, inside a NanoClaw
  checkout via `provision.sh check`); `make check`.
- Verified: API state and persistence (record on quota only, counters equal today's
  path, offer, take-once, lease fence, fallback consumes) at unit level against
  SQLite.
- Not verified: the channel tests and TypeScript checks were not run offline; no
  session was started from a native id; no two-account disposable node ran; the
  mid-session credential swap and any real quota notice are untested.
