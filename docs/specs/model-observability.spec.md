# Model observability

# 1. Purpose and Scope

Answer, for every task, **which model really ran** and **why that one**, in two
layers that are never merged:

- **harness**: the NanoClaw agent runtime (`runtime`, `engine`);
- **delegate**: the CLI implementer run by `argus-delegate`
  (`executor`: `claude | codex | opencode | agent`).

Per execution Argus stores the layer, runtime, engine or executor, provider,
requested model, configured model, observed model with its **evidence**,
timestamps and correlation ids (task, attempt, delegation id, account id, usage
`run_key`, policy revision). Per decision it stores a durable **timeline**:
initial selection, alternatives considered, coded reason, from/to, outcome.

Readable by any valid token, like every task read. Written only by the agent
holding the lease of the attempt. Phase 1 shipped the API; phase 2 adds the
producers (the NanoClaw channel for the harness, `argus-delegate` relayed
through it for the delegate) and the dashboard views.

# 2. Non-Goals

- No change to selection priorities, [model policies](model-selection.spec.md),
  provider or model pinning, or isolation.
- No inference: an observed model is never derived from pool config, policy,
  lease or argv.
- No prompts, model reasoning, tokens or credentials are stored.
- No new telemetry channel for usage, milestones or artifacts; they are reused.
- No edit or delete of history through the API.

# 3. Decisions Made

## 3.1 Evidence rule

- `observed_model` is set only with `evidence` in `reported_by_runtime |
  cli_output | provider_response`. Otherwise it is `null` with evidence
  `not_reported`: explicit unknown. The database enforces the pairing.
- `requested_model` (asked for) and `configured_model` (what the runtime was
  configured with) are separate facts and never stand in for `observed_model`.
- An observed model needs a run that started: it is refused for state
  `selected`, or when `started_at` is null. A failure before the model started
  has `started_at = null`, an `error_code` and no observation.
- Unknown is not zero: an execution without a matching account usage row has
  `usage: null`; a `not_reported` usage row keeps null token and cost fields.

## 3.2 Executions

An execution is one harness session or one delegation attempt. The producer
chooses `key` (unique per task) so that re-sending a report is safe:

- Records are append-only. A re-sent key may fill a gap or advance the state
  (`selected` < `running` < terminal); it never rewrites a stored value.
  A different non-empty value gives `409 execution_conflict`; so does a second,
  different terminal state, or a key from another attempt.
- Arguments Argus assigns itself (`started_at` on first `running`, `ended_at`
  on a terminal state) fill a gap only; a resend does not move them.
- `resumed_from` names an earlier execution of the task (resume).
- Every delegation, retry, resume and failure stays listed.

## 3.3 Selections (timeline)

Kinds: `initial`, `account_switch` (same model), `model_switch`,
`provider_switch`, `executor_switch`, `retry_no_fallback`. A kind states what
changed and is validated against the reported from/to values: a
`model_switch` needs different models; an `account_switch` needs different
accounts and cannot change model, provider or executor. An account-only switch
therefore can never be labelled a model switch.

- `reason_code`: `priority | availability | compatibility | context | quota |
  error | policy`, plus a short `explanation` (sanitised, max 300 chars).
- `alternatives` (max 10): considered options by identifier, with the code for
  why each was not chosen.
- `policy_revision` (the model-policy snapshot hash) and `execution_key`
  correlate a decision with its run; `outcome` is `unknown | succeeded |
  failed | abandoned`.
- **Argus records account switches itself.** On an account failover
  ([account selection](account-selection.spec.md)) it adds a harness selection
  with key `argus:account:<attempt>:<n>`: `account_switch` when the provider is
  unchanged, `provider_switch` otherwise. It knows accounts and providers, not
  models, so the model fields stay null.

## 3.4 Surface

- `POST /api/tasks/{id}/models` (agent token, lease-fenced: stale attempt,
  non-assignee or finished task give 409; other roles 403): body
  `{attempt, executions[], selections[]}`; returns the full trace.
- `GET /api/tasks/{id}/models` (any token): `{task_id, summary, executions,
  timeline}`, oldest first.
- `Task.models` and the task list items (REST and MCP `list_tasks`) carry the
  compact `ModelsSummary` (`harness`, `delegate` latest-run view, `executions`,
  `switches`, and `used`: each distinct layer/tool/model, at most 8, oldest
  first, the model being the observed one when reported, else the configured,
  else the requested one, with `observed` saying which); a layer with no report
  is `null`. Old tasks show nulls, an empty `used` and zeros-as-counts only.
- Executions and selections read back with the account's display name
  (`account_name`, `from_account_name`, `to_account_name`) next to its id.
- `GET /api/agents/{id}/models`: the agent's `current` model per layer and its
  `history` of executions (newest first, max 50) with evidence.
- MCP: `report_task_models` (agents), `get_task_models` and
  `get_agent_models` (any token).
- Events on the task: `task.model_execution` and `task.model_selection`,
  published once per change, carrying ids, layer, kind or state, models,
  evidence and revision; never the explanation.

## 3.5 Sanitisation and limits

- Identifiers (keys, ids, provider, models) are format-checked; one that looks
  like a credential is rejected, and the 422 does not echo it.
- Free text is whitespace-collapsed, credential-shaped parts (bearer/basic,
  `key=value` secrets, `sk-`/`ghp-`/`xox` tokens, JWTs, AWS keys, URL userinfo,
  long opaque strings) become `[redacted]`, then it is truncated.
- At most 20 records per request and 200 executions and 200 selections per
  task (`422 too_many_records`).

## 3.6 Producers (phase 2)

The agent container holds no Argus token, so it never posts. The NanoClaw
channel on the host, which holds the agent token and the leases, reports both
layers for the attempt it holds; every post is lease-fenced by the API and
best effort (a refused or failed report never fails the task).

- **Harness.** One execution per harness session of the attempt, key
  `nanoclaw:<attempt>:<n>` (`n` counts sessions within the attempt): runtime
  `nanoclaw`, `engine` the harness the leased account selected, the account's
  `provider` and `account_id`, `requested_model` / `configured_model` /
  `policy_revision` from the pinned harness model policy. It is posted
  `running` when the task is handed to the agent and ends before the reply is
  sent to Argus: `succeeded` on COMPLETE or ASK, `failed` on FAIL
  (`agent_failed`) or a provider failure notice (`quota` when the account was
  exhausted, else `provider_failure`). After an account failover the task is
  resent on a new session with `resumed_from` the previous one; Argus itself
  records the `account_switch` / `provider_switch` (3.3). The first session of
  an attempt carries an `initial` selection (reason `policy`, the model chain
  position); when the harness chain moved on because of earlier failures or
  review it is a `model_switch` (reason `error`) from the previous chain model,
  or `retry_no_fallback` when the chain has no other model. The harness does
  not expose the model that served it to the host: `observed_model` stays
  `not_reported`.
- **Delegate.** Every `argus-delegate` model attempt is one execution, key
  `dlg:<hash of run_key>`, `delegation_id` the invocation's id, `run_key` the
  account usage key: runtime `argus-delegate`, the `executor`, the executor
  account's `provider`, `requested_model` (the chain head or the override),
  `configured_model` (what was passed to the CLI; null is the CLI default),
  `policy_revision`. It is written `running` when the CLI is spawned and
  terminal when it exits (`succeeded`, `failed` with `timeout`, `exit_<n>`,
  `is_error`, `empty_result` or `empty_stdout`, `cancelled` when interrupted).
  A run blocked before the CLI started (`cwd_not_allowed`, `auth_missing`,
  `not_installed`) is `failed` with `started_at` null. Each execution has a
  selection: `initial` for model attempt 1, else `model_switch` (from the
  previous chain model, reason `error`, the fallback reason as explanation) or
  `retry_no_fallback`; its outcome is set when the run ends.
- **Observed model.** Only from what the CLI printed: Claude's
  `stream-json` `system/init` event `model` (else the dominant `modelUsage`
  entry of the result), evidence `cli_output`. Codex, Cursor `agent` and
  OpenCode outputs carry no verified model field: `not_reported`.
- **Relay.** The delegate appends one JSON line `{v, task_id, attempt, at,
  models: {executions, selections}, usage?}` to the live log relay file
  ([task live log](task-live-log.spec.md) 3.7) next to its log records; a
  record without `lines` is not a log record. The channel takes only records
  of the task and attempt it holds, only `delegate`-layer entries, at most 20
  of each, sets `account_id` / `to_account_id` to the executor account it
  leased itself (a container cannot name another account), posts
  `POST /api/tasks/{id}/models`, then the usage (`role` executor, the same
  `run_key`) to `POST /api/accounts/{that account}/usage`. The relay is
  drained before COMPLETE, FAIL or ASK, so the last run is recorded while the
  lease still holds.

## 3.7 Dashboard

- Task cards show one chip per `used` entry (`claude · sonnet-5-5`,
  `cursor · composer-2.5`); a configured-only model is marked as such, an
  unknown model reads `?`.
- The task drawer has a **Models** section from `GET /api/tasks/{id}/models`:
  per attempt, the executions (layer harness or executor, engine/executor,
  configured and observed model with evidence, account name, state, start,
  end, tokens and cost) and the selections (kind, reason, from → to, outcome,
  time). Nothing reported reads "No model reports".
- The agent drawer shows its current model per layer and its recent runs from
  `GET /api/agents/{id}/models`.
- The dashboard token is a manager token: these reads are global (the worker
  read scope does not apply).

# 4. Open Questions

- Codex, Cursor and OpenCode: once a real output sample shows a model field,
  their observation can move from `not_reported` to `cli_output`.
- Whether the harness runtime can report its observed model to the host.
- Whether cost view should group by observed model once producers report it.

# 5. Constraints

- Single node, SQLite, no new service. Writes go through `Database.write()`,
  time is `tx.now`, no network inside the transaction.
- Additive migration only; tasks created before it keep working with unknown
  values.
- Authorization matches existing endpoints: reads for any token, writes for the
  lease holder.

# 6. Implementation Consequences

- Migration `0029_model_trace.sql` (`model_executions`, `model_selections`).
- Core `argus_core/model_trace.py`; API `argus_api/modeltrace/` (service and
  routes), `Task.models`, `GET /api/agents/{id}/models`, the account failover
  hook and three MCP tools. Regenerated `openapi.json` and `schema.d.ts`.
- Phase 2: `ModelsSummary.used` and account names on reads (no migration);
  `argus-delegate` writes model records to the relay file
  (`engines/argus-delegate.mjs`, `engines/livelog.mjs`); the channel posts
  them and its own harness executions (`argus.ts`); dashboard chips, the task
  drawer timeline and the agent drawer models. Regenerated `openapi.json` and
  `schema.d.ts`. The channel change needs a NanoClaw reinstall; the delegate
  change a new agent image.
