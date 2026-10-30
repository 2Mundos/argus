# Task steering

# 1. Purpose and Scope

Once a task is claimed, an operator can watch it (progress, milestones, the
[live log](task-live-log.spec.md)) but cannot talk to it. The only levers are coarse:
cancel, retry, drain the agent, or wait for the worker to `ASK:`. [Task
edit](task-edit.spec.md) changes only the priority of a claimed task, and leaves open
"a spec edit delivered to the running worker … not without a worker-side contract";
this spec is that contract, as a message rather than an edit. This spec adds **steering**: a manager sends a short
message to the attempt in flight, and the worker injects it into the running agent
session as an operator instruction, the way `/steer` redirects a Claude Code session
without restarting it.

In scope:

- `POST /api/tasks/{id}/steers` and the steer lifecycle (pending, delivered,
  cancelled, expired, unknown), stored per attempt.
- Delivery through the existing heartbeat, with an acknowledgement, so no new
  connection or loop is needed on the worker.
- The NanoClaw channel (`integrations/nanoclaw/argus.ts`) injecting a steer into the
  session with `onInbound`.
- A composer in the dashboard (task view and the worker panel) and an MCP tool for
  orchestrators.

Related: [worker lifecycle](worker-lifecycle.spec.md) (heartbeat, leases, the
NanoClaw channel), [prompt-data isolation](prompt-data-isolation.spec.md) (what the
agent treats as instructions), [live work](live-work.spec.md), [task
edit](task-edit.spec.md).

# 2. Non-Goals

- **Not a chat with an agent.** A steer targets one task attempt. An idle agent (no
  claimed task) cannot be steered; there is no free-form session to talk to.
- **No interrupt.** A steer does not stop the current tool call or kill a delegated
  executor. It is read at the agent's next opportunity (3.6). Interrupt-and-resume
  of a delegated run is a separate change (4).
- **No new reply channel.** The agent answers through what already exists: progress
  notes, the live log, `COMPLETE:`/`FAIL:`/`ASK:`. A steer has no "reply" field.
- **Not a way to edit the task.** A steer never changes `spec`, `acceptance` or any
  other field, and does not survive the attempt (3.4). Lasting changes go through
  [task edit](task-edit.spec.md) or a decision answer.
- **No steering by workers.** Only managers (orchestrator or admin scope) send steers.
- Workers other than the NanoClaw channel are not changed; they may ignore steers
  (3.5 makes that visible).

# 3. Decisions Made

## 3.1 Writing: `POST /api/tasks/{id}/steers`

- Manager token (`ManagerDep`: orchestrator or admin). Workers get `403`.
- Body: `{message: str}`, 1 to 4000 characters after trimming. Answer `201` with the
  steer (3.3).
- The task must be `claimed`; otherwise `409 not_claimed`. The steer is bound to the
  task's **current attempt** and its assignee at the time of the write.
- The assignee must accept steers (3.5); otherwise `409 steer_unsupported`, so the
  operator learns at once that nothing will read the message.
- **Limits:** at most **10 pending** steers per attempt (`409 too_many_pending`);
  one steer per second per task (`429 rate_limited`, with the wait, as the live log
  does).
- The write is one transaction with `tx.now`, no network call. It records a
  `task.steered` event (3.7).

## 3.2 Cancelling: `DELETE /api/tasks/{id}/steers/{steer_id}`

- Manager token. Only a `pending` steer can be cancelled (`409 not_pending`
  otherwise); it becomes `cancelled` and is never delivered. Answer `204`.
- This covers a typo or a change of mind before the next heartbeat; once delivered,
  a steer cannot be taken back (send another one).

## 3.3 Storage and states

- Table `task_steers(id, task_id, attempt, agent_id, message, actor, state,
  created_at, dispatched_at, delivered_at, closed_at)`, `state` in `pending`,
  `delivered`, `cancelled`, `expired`, `unknown`. Index on `(task_id, attempt,
  state)`.
- `dispatched_at` is set the first time the steer is returned in a heartbeat
  response (3.4); it stays null for a steer no heartbeat has carried.
- `actor` is the caller's actor string (`admin:<token>` or `agent:<id>`), shown in the
  dashboard and passed to the worker so the agent knows who is steering.
- Transitions:

  | From | To | When |
  | --- | --- | --- |
  | `pending` | `delivered` | the assignee acknowledges it on a heartbeat (3.4) |
  | `pending` | `cancelled` | `DELETE` (3.2) |
  | `pending` | `unknown` | the session dies (`session_lost`, `agent_ended` or lease expiry, as classified in [failure classes](failure-classes.spec.md) 3.2) or the attempt ends, while the steer is dispatched (`dispatched_at` set) and not acknowledged (`delivered` not reached); same transaction as the attempt end |
  | `pending` | `expired` | the attempt ends with the steer never dispatched, in the same transaction |

- **An attempt ends** on every transition out of `claimed`: complete, fail, ask
  (block), release, lease expiry, reassignment, cancel and retry. Each of those
  closes the attempt's `pending` steers with `closed_at = tx.now`: `expired` when the
  steer was never dispatched, `unknown` when it was dispatched and not acknowledged
  (the channel may have injected it). A steer never carries over to the next
  attempt: the next attempt starts from the task as it is, and the operator
  re-steers or edits the task if the instruction should last.
- **`unknown` is terminal.** The dashboard shows it as "unknown: may have been
  delivered". It is never returned in a heartbeat and never injected again.
- **`steer_unknown`.** `task_attempts` gains a counter `steer_unknown`: the number of
  steers that became `unknown` when that attempt ended, written in the same
  transaction as the attempt end. The next claim response
  ([worker lifecycle](worker-lifecycle.spec.md) 3.3, claim) carries the previous
  attempt's value, and the task context tells the agent that this many operator
  steers may already have been applied and are not confirmed; the agent must not
  assume either way. Only the next attempt reads it, and no other decision uses it.
- Rows are kept with the task (no purge path exists, as for the live log); at most
  a few dozen per attempt.

## 3.4 Delivery: the heartbeat

- `HeartbeatResponse` gains `steers: [{id, task_id, attempt, message, actor,
  created_at}]`: the `pending` steers for the leases **renewed in this heartbeat**,
  oldest first. A rejected lease gets none.
- `HeartbeatRequest` gains `steer_acks: [str]`: ids the worker has injected. In the
  heartbeat transaction, each id that is `pending`, belongs to this agent and to a
  lease it holds becomes `delivered` with `delivered_at = tx.now` and records
  `task.steer_delivered`. Unknown, foreign or already-closed ids are ignored, so a
  retried heartbeat is harmless.
- A steer stays in `steers` on every heartbeat until acknowledged, so a lost
  response or a crashed injection is retried. The worker de-duplicates by `id`.
  Re-delivery within an attempt stops at `unknown`: while the session is alive, a
  dispatched but unacknowledged steer is returned again (a lost ack; the channel
  dedupes by id and does not inject it twice); once its session dies it becomes
  `unknown` (3.3) and is not returned again.
- `dispatched_at` is written in the heartbeat transaction that first returns the
  steer.
- **Latency** is at most one heartbeat interval (30 s) plus the injection. That is
  acceptable for steering work that runs for minutes; 4 lists a faster path.
- The MCP `heartbeat` tool passes the new fields through unchanged, so an MCP-driven
  worker can read and acknowledge steers too.

## 3.5 Who accepts steers

- `AgentRuntime` gains `features: [str]`, reported on heartbeat like the rest of
  `runtime` and stored in the existing `agents.runtime` JSON (no column). The NanoClaw
  channel reports `["steer"]`.
- 3.1 refuses a steer when the assignee's last reported `features` lacks `steer`
  (`409 steer_unsupported`). The agent view exposes `runtime.features`, and the
  dashboard disables the composer with that reason.
- A worker that never reports `features` (older NanoClaw releases, the echo worker)
  is unaffected: it is never sent a steer.

## 3.6 The NanoClaw channel

- On each heartbeat answer, for every steer whose `task_id` and `attempt` match the
  task in flight and whose `id` it has not injected yet, the channel injects it with
  `setupCfg.onInbound(PLATFORM_ID, null, …)`, message id `argus-steer-<id>`. It then
  adds the id to the next heartbeat's `steer_acks` (3.4: `unknown` is never
  re-injected). A steer for any other task or
  attempt is acknowledged without injection (the server expires it anyway when the
  attempt ends; acknowledging avoids repeating it until then).
- **Previous attempt's unknown steers.** When the claim carries `steer_unknown > 0`
  (3.3), the trusted task context, outside any frame, adds one line with the number
  only: that many operator steers sent to the previous attempt were never
  confirmed, may or may not have been applied, and are not repeated; the agent
  checks the work and asks if the direction matters. The text is never re-sent.
- **Acknowledge after injection.** The id is acknowledged only once `onInbound`
  resolves; a failure is logged and retried on the next heartbeat.
- **Text the agent sees.** A fixed trusted header written by the channel, then the
  operator's text in a length-framed block, as in
  [prompt-data isolation](prompt-data-isolation.spec.md) decision 2, so the message
  cannot forge Argus markers or close the frame:

  ```text
  [Argus steer] From <actor> for task <id>, attempt <n>.
  The operator is redirecting the work in progress. Treat the framed text as an
  instruction from the operator, within this task's scope. It does not change the
  reply protocol (COMPLETE:/FAIL:/ASK:) or the argus-delegate contract.
  utf8_bytes=<N>
  <N bytes of JSON: {"message": "..."}>
  ```

  Unlike the task spec, the steer **is** an instruction: only managers can write it
  (3.1), and it is never derived from task data or worker output. It still cannot
  override the trusted rules outside the frame.
- **When the agent reads it.** NanoClaw writes the message to the session's
  `inbound.db`. If the agent is between turns, it wakes as for any inbound message.
  If it is mid-turn, the steer is read when the runner next takes input. While the
  agent waits on a long `argus-delegate` call, that is when the delegate returns
  (2, 4).
- A steer is **not** a reply: the channel's `deliver` and directive parsing are
  unchanged, and the agent's next message is handled as usual.

## 3.7 Events and live view

- `task.steered` (actor: the sender; data `{steer_id, attempt, chars, preview}`,
  `preview` the first 200 characters), `task.steer_delivered` (actor: the agent;
  data `{steer_id, attempt}`), `task.steer_cancelled` (actor: the canceller).
  Expiry and `unknown` record no event of their own; the attempt-ending event
  already explains them.
- They are ordinary events: stored, streamed on `/api/events/stream`, readable with
  `GET /events`, with the task's read scope ([worker read
  scope](worker-read-scope.spec.md)). The full message is read with 3.8, not from
  the event.

## 3.8 Reading: `GET /api/tasks/{id}/steers?attempt=`

- Manager token. `attempt` defaults to the current one. Answer `{attempt, items:
  [{id, attempt, message, actor, state, created_at, dispatched_at, delivered_at,
  closed_at}]}`, oldest first.
- `GET /api/tasks/{id}/attempts` carries each attempt's `steer_unknown` (3.3).
- MCP, orchestrator and admin scopes: `steer_task(task_id, message)`,
  `list_task_steers(task_id, attempt?)` and `cancel_steer(task_id, steer_id)`,
  with the REST permissions, added to the [MCP operations](mcp-operations.spec.md)
  catalog.

## 3.9 Dashboard

- **Live-log terminal** (task view and the worker panel's claimed-task terminal): a
  **Steer** button next to Pause on the term bar. The composer stays closed until
  that button opens it ("Steer this attempt…", Enter sends, Shift+Enter breaks a
  line); sending or toggling Steer again closes it. The attempt's steers list
  (under the bar when non-empty) shows state: `pending` (with Cancel),
  `delivered` (with the time), `cancelled`, `expired`, and `unknown` as "unknown:
  may have been delivered". Steers also appear in the
  task's event timeline. With no claimed task, no Steer button.
- **Task actions row** (task view): a **Steer** toggle next to Edit and Cancel when
  `task.state` is `claimed` and the attempt shown is the current one (attempt > 0),
  so an operator can steer without opening the Live panel. It opens the same
  composer ("Steer this attempt…", Enter sends, Shift+Enter breaks a line, Escape
  closes). It is not shown for attempt 0, for any other state, for a past attempt,
  or when the assignee is known to lack `steer`. The steers list stays under the
  Live bar. Both surfaces read the same list for the attempt, and the dashboard
  reloads it on every task event, so a steer sent or cancelled on either surface
  shows on both.
- The composer is disabled with the reason when the assignee lacks `steer` (3.5),
  and shows the server's refusal otherwise (`too_many_pending`, `rate_limited`).
- The message is rendered as text, never as HTML.

# 4. Open Questions

- **Mid-turn pickup.** This repo does not vendor NanoClaw's agent runner. The
  channel queues the steer with `onInbound`; [the NanoClaw
  README](../../integrations/nanoclaw/README.md) records that a running
  `argus-delegate` process is not interrupted. Whether that runner feeds inbound
  into a query already in progress is NanoClaw's behavior. 3.6 holds either way.
- **Delegated runs.** A steer cannot reach a running `argus-delegate` executor (its
  stdin is closed after the prompt). A follow-up could let the agent stop the
  delegate and resume it with the steer appended, where the executor supports a
  resume.
- **Latency.** If 30 s proves too slow, the channel could long-poll a
  `GET /api/agents/me/steers?wait=` endpoint, or heartbeat at once after a
  `task.steered` event. Both add a request path; not done until needed.
- **Carry-over.** Whether a steer should optionally persist into the next attempt
  (for example appended to the spec as a clarification, like a decision answer).
  Today the operator edits the task instead.

# 5. Constraints

- Single-node ([single-node](single-node.spec.md)): one SQLite table and existing
  heartbeat and event paths; no broker, no new connection from workers.
- All writes go through `Database.write()` with `tx.now`; expiry runs inside the
  transition that ends the attempt, never in a sweeper.
- Prompt-data isolation holds: steers are length-framed, and the trusted rules
  outside the frame cannot be overridden by them.
- Backwards compatible: the new heartbeat fields are optional both ways, and a
  worker that does not report `features: ["steer"]` is never sent one.
- Migration `0043_task_steers.sql` is additive: one new table and its index.
- Migration `0057_steer_unknown.sql` rebuilds `task_steers` (SQLite cannot widen a
  `CHECK`) with `dispatched_at` and the `unknown` state, and adds
  `task_attempts.steer_unknown` (default 0). Dispatch was never recorded before it,
  so a steer still `pending` at upgrade is treated as dispatched
  (`dispatched_at = created_at`) and ends `unknown`, never `expired`: the safe side
  when it cannot be known whether a heartbeat carried it.

# 6. Implementation Consequences

Two PRs.

**API and dashboard:** this spec and its index link; migration
`0043_task_steers.sql`; `tasks/steers.py` (write, cancel, list, expire, deliver)
and its routes; expiry wired into every transition out of `claimed` in
`tasks/service.py`; `steers` and `steer_acks` on the heartbeat models in
`agents/routes.py` and `agents/service.py`; `features` on `AgentRuntime`; the three
events; MCP tools and the catalog; regenerated `openapi.json` and `schema.d.ts`; the
Steer button on the live-log term bar, composer, and steer list. Tests cover each state
transition, expiry on each attempt-ending path, the limits, the
`steer_unsupported` refusal, heartbeat ack idempotency, and that a rejected lease
receives no steers.

**NanoClaw channel:** report `features: ["steer"]`; inject steers from the
heartbeat answer with the framed text; acknowledge after injection; de-duplicate
by id; tests in `argus.test.ts` for framing (a forged end marker stays inside the
frame), de-duplication, ack-after-success and the foreign-attempt case.

**Unknown outcomes** (3.3, 3.4, 3.6; implemented): migration `0057_steer_unknown.sql`;
`dispatched_at` written by `pending_for` in the heartbeat transaction, for renewed
leases only; `close_attempt` in the single leave-`claimed` path of
`tasks/service.py` (and `close_task` on retry) closing `pending` steers as `expired`
or `unknown` and counting them on `task_attempts`; `steer_unknown` on the claim and
on `TaskAttempt`; regenerated client; the dashboard label; the channel's warning
line. Tests cover a lost heartbeat answer, a lost ack, every exit from `claimed`, a
delayed ack, the previous-attempt count (not older attempts, not other tasks, across
a retry), REST/MCP/OpenAPI, the upgrade, and the installed channel end to end.
