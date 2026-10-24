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
holding the lease of the attempt (phase 1 ships the API; the producers in
NanoClaw and `argus-delegate`, and the dashboard, are later phases).

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
- `Task.models` and the task list items carry the compact `ModelsSummary`
  (`harness`, `delegate` latest-run view, `executions`, `switches`); a layer
  with no report is `null`. Old tasks show nulls and zeros-as-counts only.
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

# 4. Open Questions

- Which CLIs expose a provider-observed model id is up to the producers
  (phase 2); until they report it, it stays `not_reported`.
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
- Follow-ups: NanoClaw and `argus-delegate` send the ingestion payload with
  evidence; dashboard cards, drawer timeline and Fleet current model.
