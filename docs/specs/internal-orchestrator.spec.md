# Internal Orchestrator

# 1. Purpose and Scope

Today an orchestrator always starts outside Argus: an admin mints a bootstrap
token with `role: orchestrator` and wires an agent to the MCP server
([worker lifecycle](worker-lifecycle.spec.md) 3.6). A fresh install therefore
does nothing with a goal until someone builds that setup. This spec lets Argus
**host its own orchestrator**, and makes a **chat** with it the main page of the
dashboard.

The internal orchestrator is an LLM agent with the **same contract** as an
external one: the `orchestrator` scope, the Argus MCP tools and the
`argus-orchestrator` skill ([MCP operations](mcp-operations.spec.md) 3.4). It is
not a new rule engine; deterministic recovery stays in
[failure classes](failure-classes.spec.md), the reaper and the reconciler.

In scope:

- A system-owned orchestrator pool, its template, and its enable switch.
- Conversations and messages: storage, the `argus` channel transport, the
  dashboard chat page and the **Updates** thread.
- Its authority (the orchestrator scope, minus fleet scaling) and attribution.
- Spend: usage per turn, a daily cap, cost per conversation.
- Improvements to the shared `argus-orchestrator` skill, and how they are measured.

Getting a first account, worker and orchestrator is the
[onboarding](onboarding.spec.md) spec. Related: [task steering](task-steering.spec.md)
(the closest existing message path), [task cost](task-cost.spec.md),
[settings](settings.spec.md), [home](home.spec.md).

# 2. Non-Goals

- **Not a replacement for external orchestrators.** They keep working, and may run
  next to the internal one. Concurrent managers are already safe
  (`expected_version`, `request_id` replay, `decision_not_open`).
- **No in-process agent loop.** Argus does not call a model API from the control
  plane to orchestrate; the orchestrator runs in a container like any worker
  ([single node](single-node.spec.md) 3.1).
- **No fleet scaling by the internal orchestrator** in this version: no launch,
  scale, pool create or requeue-recycle (3.4).
- **No chat for external orchestrators or workers.** Chat is between admins and
  the internal orchestrator only.
- **No actions from the chat UI.** No accept, approve or answer buttons in a
  thread; those stay in Inbox and Tasks, where versions and evidence are checked.
- **No token streaming** of replies.
- **No `goal` task kind.** A goal is what an admin asks in a conversation (3.6).
- **No deletion** of conversations or messages; archive only.
- **Off by default.** Nothing runs or spends until an admin enables it
  ([single node](single-node.spec.md) 3.5, in spirit: it spends quota unasked).

# 3. Decisions Made

## 3.1 Hosting: a system pool

- A new template **`nanoclaw-orchestrator`** in `fleet/templates.py`, with the same
  readiness gating as the worker templates ([worker launch](worker-launch.spec.md)).
  It builds a NanoClaw pool whose agents register with **`role: orchestrator`**:
  the reconciler mints the instance's bootstrap token with the pool's role instead
  of always `Role.WORKER`.
- The pool is **system-owned** (`pools.system = 'orchestrator'`): `max_replicas: 1`,
  it cannot be deleted, and `scale_pool`, `launch_worker` and pool deletion answer
  `409 system_pool`. An admin may change its image in the pool editor; nothing else.
  It is excluded from the "first worker" fact in onboarding and from worker-pool
  totals, and listed apart on the Pools tab.
- **Enable switch:** the global setting `orchestrator.enabled` (bool, default
  `false`) is the only source of truth. Writing `true` creates the pool from the
  template if missing and sets `desired_replicas` to 1; writing `false` sets it to
  0, which drains: the turn in flight finishes, new messages queue (3.3).
- The orchestrator **leases accounts like any agent**
  ([account selection](account-selection.spec.md)), preferring the setting
  `orchestrator.model` (a harness engine/model; default: the best-priority
  harness-capable account's default model). If it cannot be served, ordinary
  fallback applies. Having no task, it leases its engine with no `task_id`
  ([account selection](account-selection.spec.md)). The channel leases and
  applies the account to its OneCLI agent (selective mode) before it claims the
  first chat message, and retries with backoff while none is usable, so a turn
  never runs on whatever the vault would inject by default.
- **Model preference.** `orchestrator.model` is `<engine>/<model>` with a harness
  engine (`claude`, `codex`). The task-less lease first tries that engine's
  provider; when none of its accounts can serve, the ordinary ranking applies.
  A lease on the preferred provider carries `model`, and the channel configures
  it; otherwise it configures the host model policy's default harness model for
  the leased engine. Any other value is no preference.
- **Failover.** A chat turn whose run fails on a provider limit reports the
  account exhausted with no `task_id` (system pool engine only, like the lease),
  leases the next account, applies it and sends that turn again once, prefixed
  with a switch notice; the failure is not posted as the reply. If the switch
  stopped sessions (a new harness or model), the other turns in flight are sent
  again too, and a turn that started before the switch is only sent again. With
  no account left, the reply names the earliest reset and chat waits until a
  lease succeeds (3.3, offline). The failover event is on the agent, not a task.
  A rejected credential records `account.auth_rejected` (no task), does not
  exhaust the account, and the reply names the account so an admin can sign it
  in again. Any other failure is replied as it came, naming the provider's API
  error when the session recorded one ([worker lifecycle](worker-lifecycle.spec.md)
  3.12.1, "Provider API error").
- The skill is loaded at session start from the server's
  `skill://argus-orchestrator/SKILL.md` resource, not baked into the image, so it
  always matches the running server.

## 3.2 Conversations and messages

- Table **`conversations`**: `id`, `kind` (`chat` | `updates`), `title`,
  `title_source` (`orchestrator` | `admin`), `created_by`, `archived_at`,
  `session_ref` (the NanoClaw chat id), `created_at`, `updated_at`. Exactly one
  `updates` conversation exists; it is created with the system pool and is never
  archived.
- Table **`messages`**: `id`, `conversation_id`, `author` (`admin:<label>` |
  `orchestrator:<agent id>` | `system`), `body` (Markdown), `state` (`queued` |
  `delivered` | `answered` for human messages; `posted` for others), `truncated`,
  `turn_id`, `created_at`, `delivered_at`.
- **Who may write:** admin tokens only (`POST /api/conversations`,
  `POST /api/conversations/{id}/messages`). Orchestrator and worker tokens get
  `403`. Any admin may read every conversation.
- **Limits:** human messages 1 to 8000 characters after trimming; at most 20
  `queued` per conversation (`409 too_many_queued`); one message per second per
  conversation (`429 rate_limited`, with the wait). Orchestrator replies are capped
  at 32 KB; a longer reply is cut and stored with `truncated: true`.
- Bodies pass through the same redaction as the [task live log](task-live-log.spec.md)
  before storage.
- **Titles:** the first orchestrator reply proposes a title; an admin may rename
  (`PATCH /api/conversations/{id}` `{title}`), after which the orchestrator never
  renames it.
- **Archive:** `POST /api/conversations/{id}/archive` and `.../unarchive`. Archived
  conversations leave the list; their messages stay.
- Events: `conversation.created`, `conversation.updated`, `chat.message`,
  `chat.turn` (`started` | `finished`). The dashboard reads them over the existing
  SSE stream and resumes from `Last-Event-ID`.

## 3.3 Transport: the `argus` channel

- Chat travels only through the NanoClaw `argus` channel
  (`integrations/nanoclaw/argus.ts`). **No MCP chat tools** are added; the
  orchestrator never calls a tool to read or post chat.
- **Inbound:** the channel long-polls `GET /api/agents/me/chat?wait_s≤30` (the
  `claim_task` pattern) for messages of its conversations. Each is injected into
  that conversation's session with `onInbound` and acknowledged
  (`state: delivered`). Delivery is idempotent by message id.
- **Outbound:** `deliver()` posts to `POST /api/agents/me/chat/{conversation_id}`,
  which stores the reply and marks the human messages of that turn `answered`.
- **Trust:** a human chat message is a **trusted operator instruction** from an
  admin, framed as such. A `system` message is a server notice, not an admin
  instruction. Everything the orchestrator reads through tools (task
  specs, results, artifacts, knowledge) stays untrusted DATA
  ([prompt-data isolation](prompt-data-isolation.spec.md)).
- **Offline:** while the orchestrator is not live (disabled, starting, lost, every
  account in quota, daily cap reached), messages stay `queued` and are delivered
  in order when it returns. The chat header shows its liveness and the reason
  from the existing agent, instance and account state.
- **Progress:** while a turn runs, the thread shows one "working…" line with the
  orchestrator's latest live-log activity ([live work](live-work.spec.md)).

## 3.4 Sessions and concurrency

- One container, **one NanoClaw session per conversation** (`session_ref`), so
  contexts do not mix.
- The **Updates** session runs the inbox loop of the skill: `get_inbox` → work it →
  `wait_for_events(inbox=true)` → repeat. Proactive posts ("3 reviews wait on a
  human", "the tasks from *Fix login* are done") are a `deliver()` to Updates.
  Updates also accepts admin messages.
- **Inbox wake.** When a decision opens with audience `any` or `orchestrator` and
  `orchestrator.enabled` is true, Argus queues one message on Updates (`author:
  system`, `state: queued`) so the chat long-poll delivers it at once. The message
  names the decision id, kind and audience, and does not copy the question text.
  A notice already queued is not duplicated; once that notice is claimed, the next
  such decision queues another. Audience `human` does not wake it. If Updates
  already holds 20 queued messages, the decision still opens and the notice is
  skipped. The channel frames that `system` message as a server notice.
  A wake turn that is never answered expires like any stale turn; that does not
  confirm the decision was answered. The inbox wait of the skill stays the
  fallback for any decision still open.
- At most **one turn in flight per conversation**; later messages queue behind it.
  At most `orchestrator.max_concurrent_turns` (default 2) conversation turns run at
  once, plus the Updates loop.
- **Restart continuity:** after a container recreate (crash, account swap, image
  roll), the channel resumes the native session if it is still valid. Otherwise it
  starts a new session seeded with a bounded excerpt: the last 20 messages and the
  list of tasks created from that conversation (3.6). The thread then shows a
  `system` message "session restored from history".

## 3.5 Authority and attribution

- The internal orchestrator holds exactly the `orchestrator` scope
  ([worker lifecycle](worker-lifecycle.spec.md) 3.6), with one REST-enforced
  restriction: an agent of the system pool gets **`403 internal_orchestrator`** on
  `launch_worker`, `scale_pool`, `create_worker`, setting `desired_replicas`,
  `recycle_instance` with `requeue=true`, and `drain_agent`. It keeps read-only
  fleet diagnosis and `recycle_instance` without requeue. Hiding a tool is not
  access control; the check is in the route.
- It answers `orchestrator` and `any` audience decisions. `human` ones stay for
  admins. An `any` decision it cannot settle stays open for an admin.
- Its actions are attributed `orchestrator:<agent id>` as today, and events carry
  `actor_internal: true` when the agent belongs to the system pool, so the Events
  view can filter "what the internal orchestrator did".

## 3.6 Goals come from conversations

- A request in a conversation is the goal. Tasks the orchestrator creates while
  working a conversation turn carry **`origin_conversation_id`** (nullable column on
  `tasks`, set by the server from the turn the agent is in, not from a tool
  argument). Tasks created in the Updates loop carry the Updates id.
- A thread lists the tasks it originated (chips with state). Task detail links back
  to its conversation.
- External orchestrators and humans create tasks as today, with
  `origin_conversation_id` null.

## 3.7 Spend

- The channel reports token usage per turn, as attempts do
  ([task cost](task-cost.spec.md)); Argus prices it with the same price table and
  unpriced-model handling, attributed to the conversation (or Updates), never to a
  project's task costs.
- **Daily cap:** `orchestrator.daily_budget_usd` (number, default `5`). When the
  estimated spend since 00:00 UTC reaches it, Argus stops delivering messages
  (they queue), the Updates loop pauses, and a `system` message is posted to
  Updates. It resumes at the next UTC day or when an admin raises the cap. An
  unpriced model counts as unknown, and the cap then says so instead of treating
  the spend as zero.
- Each thread header shows its estimated cost; the Cost tab gets an
  **Orchestrator** row next to the projects ([cost view](cost-view.spec.md)).

## 3.8 Dashboard

- With `orchestrator.enabled`, the default route is **`#chat`**; otherwise
  `#dashboard` (the renamed Home, [onboarding](onboarding.spec.md) 3.4). With the
  orchestrator off, `#chat` shows an empty state that opens onboarding step 3.
- Layout inside the existing three-column shell: the left nav stays; a
  conversation list (Updates pinned first, then by recency, with **New chat**)
  sits beside the thread; the thread and composer are centred with the
  orchestrator status and conversation cost in the header; the right worker rail
  stays. At 390 px the conversation list becomes a drawer.
- **Verification limit.** The header status is registration and heartbeat
  evidence from the system pool, its instances and the orchestrator agent
  (`Registered`, `Starting`, `Draining`, `Stale`, `Lost`, `Ended`
  for a gone agent, `Offline`, `Unreachable`, and `Status unknown` while a read
  loads or after one fails, even when an older result is still held). It never says
  "Live" or "ready": no API exposes the daily budget spend against its cap, so the
  header says the budget is unknown, and it shows harness account capacity only
  from the engine catalog (`leasable`; `busy` means at `max_leases`; executor-only
  and repository accounts do not count; missing data is unknown, not free).
  Showing a verified ready/capped state needs a readiness signal from the API and
  is separate work.
- Replies render as Markdown; task, decision and agent ids render as the existing
  chips (links). No action buttons.

## 3.9 The `argus-orchestrator` skill

One skill for every orchestrator, still one file, still served unchanged
([MCP operations](mcp-operations.spec.md) 3.4), still checked by the test that
fails on a tool name that does not exist. It gains:

- **Planning playbook** (core): size a task for one worker and one reviewer; a
  spec and acceptance template; `depends_on` vs `parent_id`; how to pick a
  difficulty.
- **Reporting back** (core): in a thread, say what was created (as ids), what
  waits on a human and why; watch the created subjects with `wait_for_events` and
  close the loop when they finish.
- **Confirm before acting:** post the plan and wait for an explicit go before
  creating tasks when the plan has more than `orchestrator.confirm_above_tasks`
  (default 3) tasks, any `hard` task, or a `budget_usd` above
  `orchestrator.confirm_above_usd` (default 10). Read the thresholds with
  `get_settings`. This is guidance, not a REST rule.
- **Cost awareness:** set `budget_usd` on large tasks; read the task's cost before
  retrying an expensive one.
- **Coverage** of steering, routines, independent review and attachments.
- **An internal section** that applies only to the internal orchestrator: chat
  etiquette, the Updates thread, `origin_conversation_id`, and the 3.5 limits.
- **A tighter structure:** short rules first, tables kept, a smaller prompt.

## 3.10 Settings

| Key | Type | Default |
| --- | --- | --- |
| `orchestrator.enabled` | bool | `false` |
| `orchestrator.model` | engine/model or null | `null` (best-priority harness account) |
| `orchestrator.daily_budget_usd` | number ≥ 0 | `5` |
| `orchestrator.max_concurrent_turns` | int 1–4 | `2` |
| `orchestrator.confirm_above_tasks` | int ≥ 0 | `3` |
| `orchestrator.confirm_above_usd` | number ≥ 0 | `10` |

All are global only, admin-written, and follow the [settings](settings.spec.md) registry rules.

# 4. Open Questions

- **Fleet scaling.** When the internal orchestrator has a track record, lift the
  3.5 restriction (perhaps up to `max_replicas` only, as for external ones).
- **Native resume validity.** Whether a NanoClaw/Claude/Codex session survives a
  credential swap is the open question of the
  [quota resume research](../research/2026-10-29.quota-resume.research.md); 3.4
  falls back to seeding either way.
- **Mid-turn messages.** Whether a message sent during a turn should be injected
  into it (like a steer) instead of queuing behind it.
- **Token streaming.** Would need a streaming path through `deliver()`.
- **More than one internal orchestrator** (per project) is not considered.

# 5. Constraints

- Single node: two new tables, one column, the existing channel, heartbeat, event
  bus and SSE paths. No broker, no new service, no model client in the API process.
- All writes go through `Database.write()` with `tx.now`; the daily cap uses
  `tx.now` for the UTC day.
- Additive migrations only: `conversations`, `messages`, `tasks.origin_conversation_id`,
  `pools.system`, and orchestrator usage rows.
- Prompt-data isolation holds: only admin chat messages are trusted instructions.
- Off by default; an install that never enables it sees no behaviour change except
  the `#home` → `#dashboard` rename.

# 6. Implementation Consequences

Phase 3 of the plan in [onboarding](onboarding.spec.md) 6 (after the spec PR and
onboarding). One API + dashboard PR and one NanoClaw channel PR, then the skill PR.

**API and dashboard:** migration for the tables and columns above; the
`nanoclaw-orchestrator` template and the reconciler minting the pool's role;
`system_pool` and `internal_orchestrator` refusals in the fleet routes; the six
settings; conversation routes, agent chat routes, limits and redaction;
`origin_conversation_id` set from the active turn; usage per turn and the daily
cap; events; the `#chat` route, conversation list, thread, composer, status
header and cost; the Cost tab row; regenerated `openapi.json` and `schema.d.ts`.
Tests: role minting, every refusal, enable/disable converging the pool, queueing
while offline, per-conversation ordering and the concurrency cap, limits,
truncation, redaction, the cap pausing and resuming, `origin_conversation_id`
on created tasks, and an inbox decision of audience `any` or `orchestrator`
waking Updates once while the orchestrator is enabled.

**NanoClaw channel:** a session per conversation, inbound long-poll with
idempotent ack, `deliver()` to the agent chat route, per-turn usage, resume then
seed on restart; tests in `argus.test.ts`.

**Skill:** 3.9 rewritten in `apps/api/src/argus_api/skills/argus-orchestrator/SKILL.md`;
`apps/api/bench/mcp_inbox.py` gains a chat journey and a planning journey with
deterministic checks on the tool trace: every accept follows an evidence read,
no bulk answers, a plan is posted before more than the threshold of creates, and
created tasks carry `origin_conversation_id`.

## Verification boundary

- Commands: `make check`; the API tests above; `argus.test.ts`;
  `uv run python apps/api/bench/mcp_inbox.py`.
- Verified: storage, routes, refusals, routing and channel behaviour at the unit
  and fake-API level; skill behaviour only at the fixture level (scripted tool
  traces against seeded data).
- Not verified: orchestrator behaviour with real inference. A manual procedure
  (a runbook: enable on a disposable node, run the bench scenarios through a real
  session, record the traces) is required before the planning and confirmation
  behaviour can be called shipped.
