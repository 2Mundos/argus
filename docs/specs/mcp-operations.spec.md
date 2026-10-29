# MCP operations

# 1. Purpose and Scope

Let an orchestrator agent run Argus through MCP alone, for two journeys, with no
ad hoc REST scripts:

1. **Inbox:** list what is in review, blocked or awaiting approval; read the
   selected questions and evidence; answer, accept or reject; read the effects back.
2. **Fleet:** inspect pools, instances and agents as they were actually observed;
   change operational options, drain workers and recycle idle ones safely; wait
   for the result.

Responses must be **compact by default, complete on demand**. Every list returns a
total, an explicit page and an "omitted" note, so a page is never mistaken for
the whole set.

Every new capability is a REST endpoint first. The MCP tool is a thin call to it,
as worker lifecycle 3.7 requires. Dashboard, REST and MCP share one service and
one authorization rule.

## Coverage audit (2026-10-18, `a790f47`)

MCP has 29 tools. `tools/list` returns all of them to every token, worker or
admin, about 18.7 KB of schema. The largest are `create_task` (1.4 KB),
`route_task`, `complete_task` and `create_worker`.

| Need | REST today | MCP today | Gap |
| --- | --- | --- | --- |
| List tasks | `GET /tasks?state&limit` (≤500) | `list_tasks(state, limit)` | no project/kind filter, cursor, total, or projection; every row carries full `spec` and `result` |
| Read tasks | `GET /tasks/{id}` | `get_task` | one per call; no projection |
| Edit a task | `PATCH /tasks/{id}` | `edit_task` | covered by [task edit](task-edit.spec.md): same fields, version precondition and errors |
| Unblock / retry | `POST /tasks/{id}/unblock`, `/retry` | — | **not exposed** |
| Decisions | `GET /decisions?state&subject_id`, `GET /decisions/{id}` | `list_decisions()` (open only, no args) | filters **not exposed**; no project/kind filter; no `get_decision` |
| Answer | `POST /decisions/{id}/answer` | `answer_decision` | a retry fails with `409 decision_not_open` (no idempotent replay); one at a time |
| Artifacts | `GET /tasks/{id}/artifacts`, `GET /artifacts/{id}/content` | upload only | list and read **not exposed**; no range read; no integrity check |
| Agents | `GET /agents`, `/agents/{id}`, `POST /agents/{id}/drain` | `list_agents` | get and drain **not exposed** |
| Pools | `GET/PUT /pools/{name}`, scale, launch | `list_pools`, `scale_pool`, `launch_worker` | `PUT` replaces the whole pool and has no precondition |
| Instances | `GET /instances`, `/{id}`, `/{id}/logs`, terminate | — | **not exposed**; no observed image digest or version |
| Events | `GET /events?before`, SSE stream | — | **not exposed**; no bounded wait |
| Totals | `GET /overview` | — | **not exposed** |

Findings outside the gap list:

- **Credential exposure:** `Pool.env` values reach every manager-scope reader,
  including MCP `list_pools`. Templates may put tokens there.
- **Read scope:** narrowed in [worker-read-scope](worker-read-scope.spec.md):
  workers only see leased tasks/artifacts and project-scoped knowledge/events;
  orchestrators and admins stay global.

# 2. Non-Goals

- No shell, SQL, filesystem, Docker or generic-HTTP tools, and no "call any
  endpoint" escape hatch.
- No mandatory LLM summaries. Summaries are projections of stored fields.
- No "accept all" or "answer all" call. A batch takes an explicit answer for
  each id.
- No Git, PR or CI management. NanoClaw/worker runtimes own checkout,
  delegation, gates, PR and CI. Argus tracks references and state.
- No new authority. A tool never grants what REST denies: no Production access,
  no budget or attempt expansion, no product decisions.
- No new required service, no distributed architecture (single-node spec).
- Browser QA, human consent and environment access stay task-specific.

# 3. Decisions Made

## 3.1 Shared conventions for list reads

- **Page envelope** for every new list:
  `{items, total, has_more, next_cursor, omitted}`. `total` counts every match
  of the filters and is computed in the same read transaction as `items`, as one
  snapshot. `omitted` names the fields left out by the projection and any field
  that was truncated.
- **Keyset cursors** are opaque strings encoding `(sort key, id)` and the
  filters. A cursor reused with different filters is refused with
  `400 cursor_mismatch`. Default `limit` is 25, max 100.
- **Projections:** `view=summary` (the default) or `view=detail`. Summary carries
  ids, title, state, kind, project, age, attempt, version, and references (ids)
  to decisions, artifacts and dependencies. A task summary also keeps the short
  operational fields the board shows (assignee, lease, block reason, progress
  note). It carries no `spec`, `result`, `input`, `question` or `payload` bodies.
  Detail carries the full model, but every free-text field over **4 KB** is cut,
  and each item says so with `truncated: {field: full length in bytes}`. A
  single-item read (`GET /tasks/{id}`, `GET /decisions/{id}`) stays whole.
- **Summary reads skip bodies.** `list_tasks` and `get_tasks` select the summary
  columns only (`LIST_COLUMNS` in `tasks/views.py`): not `spec`, `acceptance`,
  `input`, `required_capabilities`, `result`, or `error`. `view=detail` still
  returns those fields, loaded per id. The JSON shape does not change.
- **Newest-first indexes.** Partial indexes `tasks_active_newest` and
  `tasks_archived_newest` on `(created_at, id)` match `archived_at IS NULL` and
  `IS NOT NULL`. The list's `ORDER BY created_at DESC, id DESC` uses them, and
  `COUNT(*)` of active tasks uses `tasks_active_newest` (migration
  `0035_tasks_newest`).
- **Version:** each task and pool exposes `version` (its `updated_at`, ISO). Writes
  that need concurrency protection take `expected_version` and fail with
  `409 stale_version` and the current version.

## 3.2 P0 — inbox and evidence

- **`GET /api/inbox`** (manager scope) → MCP `get_inbox(project?, states?,
  decision_kinds?, view?, limit?, cursor?)`. Rows come from tasks in
  `blocked | in_review | awaiting_approval`, plus claimed tasks with
  `quiet_at` set ([stall visibility](stall-visibility.spec.md) 3.4), plus open
  decisions whose subject is not a task (pool policy, knowledge). Each row joins
  the task with its open decision, if any, and adds `age_s`, `decision_id`,
  `decision_kind`, `audience`, `artifact_ids` (the current attempt's) and
  nullable `last_signal_at` and `last_signal_source`. The response also carries
  `totals_by_state` and
  `totals_by_decision_kind` for the filtered set, from the same snapshot. A row
  about another subject counts under its subject type (`pool`), and a task with
  no open decision under `none`. `states` keeps only task rows. Order: oldest
  first, by when the row started waiting. Pending tasks stuck behind a failed or
  cancelled dependency are rows too
  ([task dependencies](task-dependencies.spec.md) 3.2).
- **Inbox groups and impact:** `GET /api/inbox` and `get_inbox` also take
  `blocking_only` (bool, default false) and `order` (`age` default | `impact`),
  and return the same data to the dashboard, MCP and REST.
  - *Impact* is derived only from task rows and `task_deps`. A row's `blocks` lists
    the tasks that cannot proceed until it is resolved: its own task when that task
    is `blocked`, plus the direct dependents (`task_deps.depends_on_id` = the row's
    task) that are not in a terminal state (`succeeded | failed | cancelled`). It
    is `[]` for pool and knowledge rows. Each entry is `{id, title, state}`;
    `impact` is its length. Nothing else is inferred.
  - `blocking_only=true` keeps only rows with `impact >= 1`, in `total`, totals and
    `groups` alike. `order=impact` sorts by `impact` descending, ties by age
    (oldest first); `order=age` is the existing order. The cursor carries the order.
  - Every row has `group`, `decision_kind` (the type: `question`, `review`,
    `approval`, `policy`, `knowledge`), `age_s`, `blocks` and `impact`.
  - `groups` covers the whole filtered set (not just the page), one per project and
    task: `{key, project, subject_type, subject_id, title, open_decisions,
    oldest_age_s, stalled_tasks}`. A task's rows form the group `task:<id>`; open
    pool decisions form `pool` (project `null`); knowledge proposals form
    `knowledge_proposal:<project>`. `open_decisions` counts rows with an open
    decision, `oldest_age_s` is the age of the group's oldest row, and
    `stalled_tasks` is the number of distinct tasks in the group's `blocks`.
    Groups follow the requested order (`impact`: `stalled_tasks` desc, then age).
  - The dashboard Inbox reads `/api/inbox` (detail view) and splits the rows by
    `decision_kind` (`question`, `approval`, `review`, `knowledge`, `policy`,
    then `none` for a row with no open decision). Each kind is a section with
    its count. Project headings and task groups stay inside that section. The
    manager can tick up to 20 open decisions (the `answers` batch limit). Each
    ticked decision carries its own answer (choice and/or text). A confirmation
    step lists every decision with the answer that will be sent, and sends
    `POST /api/decisions/answers` only on confirm; nothing is answered
    automatically. The result of each item is shown (`ok`, `replayed`, or the
    error message). Retrying failed items resends them with the `request_id`
    first generated for that item, so a retry never duplicates an answer.
    Accept/reject and audience rules are unchanged.
- **`GET /api/tasks`** gains `project`, `kind`, repeated `state`, `view` and
  `cursor`. It always returns the page envelope. The dashboard moves to the new
  shape in the same PR, so the generated client keeps a single response type.
  The Tasks tab requests up to five pages of 100 summaries, draws the first page
  when it arrives, and appends the rest. Changing active/archived drops the list
  on screen; a live refetch of the same list does not.
- **Batch read:** `GET /api/tasks?ids=a,b,c&view=detail` (≤50 ids) → MCP
  `get_tasks(ids, view)`. Unknown ids come back in `missing: [...]`; nothing
  fails as a whole. `ids` takes no other filter or cursor (`422 ids_exclusive`).
- **Task log:** MCP `get_task_log(task_id, attempt?, after?, limit?)` reads the live
  log tail like `GET /api/tasks/{id}/log` ([task live log](task-live-log.spec.md) 3.5),
  with REST's permissions; it is listed to every scope.
- **Task steering:** MCP `steer_task(task_id, message)`, `list_task_steers(task_id,
  attempt?)` and `cancel_steer(task_id, steer_id)` wrap the steer routes
  ([task steering](task-steering.spec.md) 3.8). Listed to orchestrators and admins.
  `heartbeat` passes `steer_acks` through and returns `steers` for renewed leases.
- **Decisions:** MCP `list_decisions` exposes REST's `state` and `subject_id`
  filters and the new `project` and `kind` filters, with the page envelope. Add
  MCP `get_decision`.
- **Artifacts:** MCP `list_artifacts(task_id, attempt?, role?)` returns metadata
  plus `in_result`, which says whether the task's result lists the artifact. It
  is the whole list for one task, not a page: a task's artifacts are bounded by
  its attempts.
- **Artifact roles:** an upload may declare an optional `role`, one of
  `report | delegate_report | policy_snapshot | qa_receipt | other`. This needs
  a migration, and existing artifacts get `null`. The role is declared at upload,
  never inferred from the name. MCP `upload_text_artifact` gains `role`.
  MCP `read_artifact(artifact_id, offset=0, max_bytes=16384)` returns UTF-8 text
  for `text/*` and JSON media types, or `binary: true` with no bytes otherwise.
  Size, SHA-256, `created_by` and the attempt come back every time. It never
  splits a UTF-8 character: a read ends before one and `next_offset` resumes
  there. REST serves it as `GET /artifacts/{id}/text?offset&max_bytes`
  (64 to 65536), and gets `Range` support on `/content` and
  `GET /artifacts/{id}` for metadata.
- **Attachments:** an orchestrator or admin can attach evidence (screenshots,
  ZIPs, logs) to any task without a worker lease. REST
  `POST /api/tasks/{id}/attachments?name&role` takes the raw body (same name
  rules, same `ARGUS_ARTIFACT_MAX_BYTES` limit and streaming as the worker
  upload); MCP `attach_artifact(task_id, name, content | content_base64,
  media_type?, role?)` wraps it and is listed to managers only. Attachments are
  stored at `tasks/{id}/0/{name}` (attempt 0 is the task-level pseudo attempt, so
  a duplicate name is `409 artifact_exists` and never overwrites), do not count
  against the agent quota, and are capped per task by
  `ARGUS_ATTACHMENT_MAX_PER_TASK` (default 50, `400 attachment_limit`). Rows carry
  `source: attachment` and `attached_by` (`orchestrator:<id>` or `admin:<label>`,
  never `agent:…`); worker rows are `source: worker` with `attached_by` null.
  Attachments are readable by orchestrators and admins only: workers get `403
  attachment_scope` on get, content, text and verify, do not see them in
  `GET /tasks/{id}/artifacts`, and cannot list them in `result.artifacts`. The
  existing read, `/content`, `/text` and `/verify` endpoints serve them. The
  `task.attachment` event records only `{source: attachment}` and the actor: no id,
  name, size or hash, so the feed never discloses evidence names or digests. Read
  the artifact for those. The dashboard
  shows name, size, media type and source for every artifact with a download.
- **Verification:** `POST /api/artifacts/{id}/verify` → MCP
  `verify_artifact`. It re-hashes the stored file and returns
  `{ok, size, sha256, expected_size, expected_sha256}`. `ok` means the bytes are
  intact. It says nothing about whether the content is acceptable.
- **Answers:** `answer_decision` gains an optional `request_id`. A retry that
  carries the same `request_id`, actor, choice and text after success returns
  the stored decision with `replayed: true` instead of `409 decision_not_open`.
  Any other difference stays a 409: another choice, text, caller or request id,
  no request id, or a decision that was cancelled rather than answered. The
  request id (≤200 characters) is stored on the decision.
  `answer_decisions([{decision_id, choice, text, request_id}])` (≤20) answers
  each item in its own write transaction and returns one compact
  `{decision_id, ok, replayed, subject_type, subject_id, answer_choice,
  decided_at}` or `{decision_id, ok: false, error}` per item, in order. It does
  not repeat the decision's question; `get_decision` reads it in full. REST serves it as
  `POST /api/decisions/answers`; a malformed or oversized batch is a 422 as a
  whole, and an item's domain error (`{status, code, message}`) fails only it.
- **Unblock and retry** are exposed as MCP `unblock_task` and `retry_task`;
  `release_task(task_id, reason, attempt?)` requeues a claimed task without
  counting an attempt ([worker lifecycle](worker-lifecycle.spec.md) 3.3).

## 3.2b Model observability

`report_task_models` (agents), `get_task_models` and `get_agent_models` (any
token) wrap the model trace endpoints ([spec](model-observability.spec.md)).

## 3.3 P1 — fleet

- **Inspect:** `GET /api/pools/{name}/inspect` → MCP `inspect_pool(name)`
  returns the pool, its instances with their agents, and a runtime record per
  instance. Each runtime field (`image`, `image_digest`, `worker_version`,
  `capabilities`, `engine`, `executor`, `ready`) carries a `source`:
  `configured | requested | started | validated | unknown`. A fact without proof
  is `unknown`. Heartbeats prove a live process, not a working model (home spec).
  `image` is `requested` (what the instance was started with, recorded at start)
  or else the pool's `configured` image. `capabilities` are `validated` when
  active agents registered them, else `configured`. `ready` is `true`
  (`validated`) only while the instance has an active agent; otherwise it is
  `unknown`, never `false` by assumption.
- **Runtime facts come from two layered sources.** The reconciler reads the image
  digest from the target (the container's image id in Docker, the container
  status `imageID` of the Pod) in the listing it already takes before each pass,
  outside any write transaction, and stores it on the instance (`started`). Agents
  may send an optional `runtime` object (`worker_version`, `engine`, `executor`)
  on heartbeat, stored on the agent (`validated`); a heartbeat without it keeps
  the last report. One migration adds both.
  Workers that do not report stay `unknown`. A worker never vouches for its own
  image.
  MCP `get_instance(id)` and `get_instance_logs(id, tail≤500)` wrap the existing
  REST calls.
- **Safe option updates:** `PATCH /api/pools/{name}` (admin, the same as `PUT`)
  with a partial body and a required `expected_version` → MCP `update_pool`.
  Fields it does not mention are kept. Name, target and runtime cannot change
  through it (any unknown field is a 422). `env` values are write-only, and
  responses return only the keys: a PATCH merges `env`, a value sets a key and
  `null` removes it. A stale version is `409 stale_version` with the current
  version in `details`; a result that breaks a pool rule is `422 invalid_pool`.
  `update_pool` is listed to admins only.
- **Model policy:** `GET/PUT/DELETE /api/models` ([model selection](model-selection.spec.md))
  → MCP `get_model_policy` (anyone), `set_model_policy(harness, implementation)` and
  `reset_model_policy` (admin only). They are thin calls to the same routes, so
  validation (422 naming the chain), the audit event and the env-wins rule are
  identical. Availability of a model at the provider is not checked.
- **Redaction:** in every pool response, on every surface, `env` comes back as
  `{key: "***"}` unless the caller is admin and asks for `reveal_env=true` over
  REST (`GET /api/pools/{name}`; anyone else gets a 403). MCP never sends that
  flag. The dashboard edits existing pools through PATCH, so it never writes a
  redacted value back.
- **Drain and recycle:** MCP `drain_agent(agent_id)` wraps the existing REST
  call. `POST /api/instances/{id}/recycle` → MCP `recycle_instance` marks the
  instance `draining`. The reconciler terminates it only once none of its agents
  holds a claimed task, then starts a replacement up to `desired_replicas`. A
  recycle never interrupts active work or a running delegate. Checkpoints stay
  the worker runtime's job. `recycle_instance(instance_id, requeue=true)`
  instead releases the instance's claimed tasks now (no attempt counted), so it
  stops on the next pass ([worker lifecycle](worker-lifecycle.spec.md) 3.5).
  Orchestrators and admins may recycle; only a
  `running` instance can be (`409 instance_not_running`), and recycling one that
  is already draining changes nothing (except that `requeue` still releases its
  tasks). The same stop rule now covers every
  draining instance, so an idle drained worker that never calls `leave` no
  longer holds its slot.
- **Bounded wait:** `GET /api/events/wait?after=<id>&wait_s≤30&subject_type&subject_id`
  → MCP `wait_for_events`. It returns the events after the cursor at once, or
  long-polls on the bus up to `wait_s`, the same pattern as `claim_task`. It
  returns `{events, next_cursor, timed_out}` with at most 100 events, oldest
  first. Without `after` it waits from the newest event. It has its own path, so
  `GET /api/events` keeps its newest-first list. REST defaults `wait_s` to 0 (a
  plain read); MCP to 20.
- **Inbox wait:** `inbox=true` on the same wait keeps only the events that add a
  row to `get_inbox` (3.2): `decision.opened`, `task.dependency_failed`,
  `task.quiet`, and a task transition whose `data.to` is `blocked`, `in_review`
  or `awaiting_approval`. It combines with the subject filters. An
  orchestrator's idle loop is `get_inbox` → work it →
  `wait_for_events(inbox=true)` → repeat, woken only when something new waits on
  it. Events that leave the inbox (answers, cancellations, a quiet claim that
  signals again) are not included; re-read `get_inbox` after waking.
- **Cursor on timeout:** when nothing matches, `next_cursor` moves to the newest
  event the wait checked, not back to `after`, so a filtered wait that idles
  does not rescan the same events on every call. Nothing is skipped: every
  event up to that cursor was already checked against the filters.

## 3.4 Discovery

- One mount at `/mcp`. A server middleware (`MCPServer.middleware`, the SDK's
  documented hook) resolves the bearer token's scope and filters the
  `tools/list` result. A missing or invalid token gets a `401 unauthorized`
  error. The SDK passes the middleware a serialized result, and tests pin that.
  If an SDK upgrade breaks the hook, the fallback is a second mount at
  `/mcp/ops`.
- Each tool declares its audience: agents (worker and orchestrator tokens),
  managers (orchestrator and admin), or anyone. Workers see the task-working
  and knowledge tools. Orchestrators see everything. Admins see everything
  except the tools that need an agent identity (`claim_task`, `heartbeat`, …).
  Admin-only tools (`update_pool`, `set_model_policy`, `reset_model_policy`) appear only for admins. A test calls every
  listed tool for each scope and fails if REST refuses one on scope. Calling an
  unlisted tool still follows REST's rules. Hiding a tool is not access control.
- The server `instructions` name the inbox journey and the fleet journey. They
  also give the fallback when a tool is missing: say which tool is missing and
  stop; do not script REST around it. The same text goes to
  `integrations/nanoclaw/README.md` for orchestrator setup.
- **Orchestrator skill.** The orchestrator's playbook (what it can, must and
  must not do with these tools) is one file in the open Agent Skills format,
  `apps/api/src/argus_api/skills/argus-orchestrator/SKILL.md`, linked from
  `skills/argus-orchestrator/` at the repo root for skill installers. The server
  serves it, unchanged, as the resource `skill://argus-orchestrator/SKILL.md`
  (`text/markdown`) and as the prompt `argus-orchestrator`, and the
  `instructions` point to it. It holds no rule REST does not enforce. A test
  fails if it names a tool that does not exist.

## 3.5 Benchmark

- `apps/api/bench/mcp_inbox.py` seeds a fixed synthetic set: 40 tasks over 3
  projects, 12 blocked with questions, 8 in review with artifacts, 4 awaiting
  approval, and 2 pool policy decisions. It then runs the inbox journey twice
  over the same expected outcome, once with the pre-spec tool set (a frozen
  script) and once with the new tools.
- The "before" side is frozen first: the journey script and a copy of the
  current tool schemas are committed before any tool changes, so later PRs
  cannot move the baseline.
- It records calls, request and response bytes, `tools/list` bytes per scope,
  cumulative context bytes and wall time. Tokens are reported as
  `bytes / 4` and labelled an estimate.
- An optional model run (same model and harness, both tool sets) is recorded by
  hand in a dated `docs/research/` note.
- The PR publishes the real numbers, including regressions such as larger
  schemas.

**Measured after step 3** (`results/after.json` against `results/before.json`,
same seed and outcome; tokens are bytes / 4):

| | Before | After | Change |
| --- | ---: | ---: | ---: |
| MCP calls | 24 | 25 | +1 |
| Ad hoc REST calls | 8 | 0 | −8 |
| Response bytes | 204,926 | 45,062 | −78% |
| `tools/list` bytes, orchestrator (tools) | 17,618 (29) | 22,812 (37) | +29% |
| `tools/list` bytes, worker (tools) | 17,618 (29) | 13,292 (21) | −25% |
| `tools/list` bytes, admin (tools) | 17,618 (29) | 16,576 (27) | −6% |
| Context bytes (est. tokens) | 227,008 (56,752) | 72,613 (18,153) | −68% |

The saving comes from summaries and server-side filters: `list_tasks` over three
states with full bodies (121 KB) and `list_decisions` (38 KB) become one inbox
page (7.8 KB) and one page of question details (6.7 KB). The cost is a larger
orchestrator schema: the 8 new tools and the wider list signatures add 5.2 KB,
paid once per session. Reviews
take three calls each (list, verify, read) instead of two REST reads, because
integrity and the `in_result` check move to the server. Read-back is one batch
call instead of ten.

**Measured after step 4** (`results/answers.json`): one `answer_decisions` call
replaces ten `answer_decision` calls, so the journey takes 16 MCP calls instead
of 25. It saves calls and round trips, not bytes: context is 76,346 bytes
(19,086 est. tokens), against 74,290 for the step 3 journey on the same tree.
Each item returns the whole decision, question included (13.4 KB for ten), and
the new tool adds 1.0 KB to the orchestrator's `tools/list` (23,825 bytes, 38
tools).

**Measured after step 6** (`results/answers-compact.json`): with compact batch
items the `answer_decisions` response drops from 13,386 to 3,580 bytes, and the
batch journey's context to 69,672 bytes (17,418 est. tokens), against 77,422 for
answering one at a time on the same tree. Against the frozen baseline (227,008)
that is −69%, in 16 calls instead of 32. The orchestrator's `tools/list` is
26,957 bytes (44 tools), `wait_for_events` included.

**Schema cost of step 5** (no fleet journey is benchmarked): the orchestrator's
`tools/list` grows to 26,148 bytes (43 tools, +2.3 KB), the admin's to 21,123
(34 tools, +3.5 KB with `update_pool`), and the worker's to 13,796 (+0.5 KB, the
heartbeat's `runtime`). The inbox journey is unchanged in calls; its context is
78,669 bytes, the larger `tools/list` included.

# 4. Open Questions

- **Worker dependency reads:** workers no longer have global task reads (see
  [worker-read-scope](worker-read-scope.spec.md)). If dependency results must be
  readable without a lease, that needs an explicit grant rule.
- **Structured summaries per artifact role:** which roles have a schema worth
  parsing into a summary. Decide once roles are in use.

# 5. Constraints

- Single process, SQLite, no new service. The bounded wait uses the
  in-process bus.
- Writes go through `Database.write()` and use `tx.now`. No target or network
  call inside a write transaction; the inspect and verify reads happen outside one.
- MCP holds no logic. Every behaviour above is tested over REST, and MCP tests
  cover forwarding, projections and errors.
- Never return credential values, auth files or raw tenant data. Return
  references and redacted values instead.
- Response caps are enforced server-side. The client cannot raise them past the
  documented maximums.

# 6. Implementation Consequences

Delivered as small PRs, in order (all done):

1. **This spec** and the index link.
2. **Baseline and discovery:** the frozen "before" benchmark (script, seed,
   schema snapshot) and the scope-filtered `tools/list`, with its measured
   saving.
3. **P0 reads:** the page envelope and projections in `tasks/service.py`
   (dashboard moved to the envelope); `GET /inbox`; batch `ids`; decision filters;
   artifact metadata, range read and verify; a migration adding the artifact
   `role`; MCP tools `get_inbox`, `get_tasks`,
   `get_decision`, `list_artifacts`, `read_artifact`, `verify_artifact`,
   `unblock_task`, `retry_task`; the "after" benchmark run.
4. **P0 answers:** a migration adding the decision `request_id`; idempotent
   replay; `answer_decisions`.
5. **P1 fleet:** a migration adding the instance image digest and the agent
   runtime; `env` redaction; `PATCH /pools` with `expected_version`;
   `inspect_pool`; the instance tools; `drain_agent`; recycle in the reconciler.
6. **P1 wait:** `GET /events?after&wait_s`, and `wait_for_events`.

Each PR updates worker lifecycle 3.7's tool list, regenerates the OpenAPI client,
and adds tests for its part of the following:
- authorization per scope;
- totals against pages;
- cursors and `cursor_mismatch`;
- projection and truncation caps;
- idempotent replay;
- `stale_version`;
- redaction;
- REST/MCP parity.
