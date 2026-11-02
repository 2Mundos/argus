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
/tasks/{id}/quota-resume`, the channel files `argus-quota-resume.ts` and
`argus.ts`, and the spike scripts under `integrations/nanoclaw/scripts/`.

# 2. Non-Goals

- No resume after `session_lost`, a crash or a stall, and no cross-node or
  cross-engine resume. Those belong to P4 of
  [worker efficiency](../drafts/2026-10-24.worker-efficiency.draft.md).
- No Codex, Cursor, Copilot, custom or local resume (3.6).
- No claim that the credential swap works inside a live session (4.2).
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
`not_available` (resume is off, 3.9), `unreadable` (the transcript file is gone or
empty). A fallback is today's fresh session with the checkpoint restored.

To resume, the channel makes the attempt's NanoClaw session start from the native
id before its container exists. It resolves the attempt's per-thread session
(`argus:<task>:<attempt>`), which creates the session row and its `outbound.db`, and
writes the agent runner's continuation (`session_state`, key `continuation:claude`)
to the offered id. At its first poll the runner resumes that Claude Code session
from the group's `.claude-shared` transcripts, which every session of the group
shares. The write happens only when the continuation is empty, there is no legacy
`sdk_session_id`, and the session has not run (no `messages_out`, no
`processing_ack`). Writing the same id again is a no-op that succeeds, so a retry
is idempotent. Any other state, an unexpected table layout, a wiring that is not
per-thread or a non-SQLite mailbox writes nothing and gives `resume_failed`.

`resumed` means the seed is in place before the container starts. The runner can
still start fresh (4.3). A fresh conversation is never reported as a resume by the
channel.

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
| Claude (`anthropic`) | Records, offers, and resumes by seeding the runner's continuation (3.4) when `ARGUS_QUOTA_RESUME=1`. |
| Codex | Unsupported. Nothing shows a thread resumes under another ChatGPT account. |
| Cursor, Copilot, custom, local | Unsupported. The channel sends no session. |

## 3.7 Observing the session id

NanoClaw's own session id is not given to the channel (a chat `session_ref` is the
thread). The Claude Code session id is visible on the host as the transcript
filename `<id>.jsonl` under `.claude-shared/projects/<project>/` of the group's
session directory. The channel takes it only when exactly one non-empty transcript
was written since the claim; zero or several mean no session is recorded.

## 3.8 Way back

Not resuming is today's behaviour, so every failure mode degrades to it. Unsetting
`ARGUS_QUOTA_RESUME` turns resume off at the next host start. There is nothing
to undo: an offered session is used once or ignored, and a seeded session is an
ordinary attempt session, closed like any other.

## 3.9 Opt-in

Resume is off unless the host process sees `ARGUS_QUOTA_RESUME=1`; any other value
is off. Off, every offer falls back with `not_available`, and recording and
offering still happen. It stays opt-in until the operator procedure (Verification
boundary, level 3) has passed on a disposable node.

# 4. Open Questions

## 4.1 Proven

At the NanoClaw pin `8fbacc9`, with Claude Code 2.1.296 and the runner's
claude-agent-sdk 0.3.296, against a local fake Messages API
([spike](../research/2026-11-02.quota-resume-spike.research.md)):

- `claude --resume <id>` keeps the id and resends the earlier turns. A fresh run does
  not. An unknown id fails before any request, with an error that the runner's stale
  session check matches.
- The pinned runner, started on a new session directory whose `outbound.db` was
  seeded by the shipped `seedClaudeContinuation`, logs `Resuming agent session
  <id>`, keeps the id, and sends the first session's turns. An unseeded session
  starts fresh. The seed is idempotent for the same id and refuses another one.
- A seed whose transcript is gone is cleared by the runner.

## 4.2 Not proven

- **Real accounts.** The spike used a fake API and a placeholder key. Whether
  Anthropic accepts a resumed conversation under a different account's credentials
  has not been run (level 3).
- **The production path end to end.** No real NanoClaw host has run `resolveSession`,
  the seed and a container start together. The pieces were tested separately:
  the channel with mocks, the runner outside a container.
- **Validity cutoff.** The only check is that the transcript exists and is not
  empty. The provider's retention of a conversation is unknown.
- **Codex with another account.** Unverified, so unsupported (3.6).

## 4.3 Risks

- **Rotation after the report.** The runner drops a continuation whose transcript is
  over 12 MiB or older than 14 days (by its first entry) and starts fresh. Argus has
  already recorded `resumed` and the agent got the note. The task text is in the
  note, so the work goes on as a cold start.
- **Transcript gone at start.** If the transcript disappears between the check and the
  container start, the runner clears the continuation, and that first batch, which
  is the task message, is acknowledged without a model call. The attempt then waits
  like any failed run (the "Not done yet" timeout in the NanoClaw README). The window
  is the seconds between the seed and the first poll.
- **Note lost, resume kept.** If the seed succeeds but the take answers `first:
  false` or fails, the session resumes without the note, and the task text arrives
  as usual.
- **Private layout.** The seed writes NanoClaw's container-owned mailbox, and only
  before any container has run. It depends on the runner's `session_state` layout and
  per-thread wiring; any change gives `resume_failed`. Re-check on every NanoClaw
  pin bump (the spike script fails if the runner's mailbox paths move).

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
- The channel's seed is `seedClaudeContinuation` (`argus-quota-resume.ts`) and the
  host wiring `resumeClaudeSessionFromEnv` (`argus.ts`), installed only by
  `quotaResumeFromEnv`. The draft is superseded by this spec.

## Verification boundary

1. **Fake and unit (done).** `uv run pytest apps/api/tests/test_account_failover.py`
   (record on quota only, counters, offer, take-once, lease fence, fallback consumes).
   Vitest `argus-quota-resume.test.ts` (decision, seed, host wiring, gate) and
   `argus-accounts.test.ts` (the channel flow: seed, note once, crash and re-claim,
   stale lease, every fallback, `resume_failed`), inside a NanoClaw checkout via
   `provision.sh check`.
2. **Disposable runtime (done).** `integrations/nanoclaw/scripts/quota-resume-spike.sh`
   runs the real `claude` CLI and a scratch copy of the pinned runner against
   `scripts/fake-anthropic.mjs` in a temp dir, with no credentials (4.1). It prints
   ids, booleans and counts only.
3. **Real two accounts (not done).** The operator runs it, on a disposable node:
   1. Use a disposable node and a test repository, with two disposable Claude
      accounts A and B in the pool, A preferred, and `ARGUS_QUOTA_RESUME=1` in the
      pool's `env`. Credentials stay in the gateway; nothing is pasted into chat or
      logs.
   2. Create a task that asks the agent to remember a random marker and reply,
      then wait for a second message.
   3. Run it while A is at a usage limit it reached in normal use. Do not generate
      traffic to exhaust it. Marking A exhausted through the API does not count:
      only a quota notice from the session itself records the session (3.2).
   4. Expected: attempt 1 ends as a quota pause with `native_session` set; attempt 2
      is claimed on the same node with B; `task.quota_resume` shows `resumed`; the
      host log shows `Resuming agent session <id>` with the attempt-1 id; the agent
      states the marker without being told it again.
   5. Abort and unset the flag if any of these happen: the claim lands on another
      node or engine, an authentication error follows the resume, a transcript or
      credential appears in any log or event, the attempt hangs after the resume, or
      the marker is missing while `resumed` was reported. Then file what was seen.
