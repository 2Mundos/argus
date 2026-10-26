# Settings

# 1. Purpose and Scope

One place for operator choices that today live in scattered env vars, files and
views: **global settings** with **per-project overrides**. A setting is a typed
key from a registry in code; its effective value for a project is the project
override, else the global value, else the registry default.

Phase 1 ships the store, the API, the Settings tab and two settings:

- **Model preferences**: the [model policy](model-selection.spec.md) moves from the
  Engines view to Settings and gains project overrides.
- **Independent code review**: a project can require that code a model wrote is
  reviewed by a different model before the task succeeds (3.5).

Readable by any valid token. Written by admins.

# 2. Non-Goals

- No free-form key/value store. Unknown keys are rejected; every key has a type,
  a default and a list of scopes it may be set at.
- No per-agent, per-pool or per-task settings. Tasks keep their own fields (pins,
  `review`, difficulty); a setting never rewrites a stored task.
- No change to a claimed attempt. Settings are read at the moment they apply
  (claim, completion); an attempt in flight keeps what it started with.
- No secrets in settings. Credentials stay in the SOPS file
  ([single node](single-node.spec.md) 3.6).
- Not a replacement for the PR's own CI or agentic review
  ([gitea helper](gitea-helper.spec.md) 3.4); that stays a separate signal.

# 3. Decisions Made

## 3.1 Registry

Each key is declared once in code with: `key` (dotted, e.g. `review.independent`),
a JSON Schema for its value, `default`, `scopes` (`global`, `project`, or both),
and a one-line description shown in the dashboard. The registry is served by
`GET /api/settings/registry`. Adding a key is a code change and a spec change.

## 3.2 Storage and resolution

- Table **`settings`**: `scope` (`global` or a project slug, FK to `projects`),
  `key`, `value` (json), `updated_at`, `updated_by`. Primary key `(scope, key)`.
  Absent row means "inherit".
- Effective value for project `p`: row `(p, key)`, else `(global, key)`, else the
  registry default. Each read reports `source`: `project` | `global` | `default`
  (and `env` where 3.4 applies).
- A value is validated against the key's schema on write (`422` naming the key and
  the problem). A key not settable at the requested scope is `422 scope_not_allowed`.
- Every write emits `settings.changed {scope, key, from, to}` in the same
  transaction; the Events view and audit trail are that table.

## 3.3 API, MCP, dashboard

- `GET /api/settings` — global values with `source`.
- `GET /api/projects/{slug}/settings` — effective values for the project, with
  `source` per key.
- `PUT /api/settings/{key}` and `PUT /api/projects/{slug}/settings/{key}` — body
  `{value}`; admin only.
- `DELETE` on the same paths — remove the row (revert to inherit); admin only.
- MCP: `get_settings {project?}` (any token), `set_setting {key, value, project?}`
  and `reset_setting {key, project?}` (admin), over the same routes.
- **Settings tab**: a scope picker (`Global` or a project). Each key shows its
  effective value, where it comes from, and a "Reset to inherited" action on an
  override. The Engines view keeps engine readiness and links to Settings for
  model chains.

## 3.4 Model preferences

- Key `models`, scopes `global` and `project`. The global value is today's policy;
  storage, routes and precedence of [model selection](model-selection.spec.md) 4a
  stay as they are (`ARGUS_MODEL_POLICY` env, then `data/model-policy.json`, then the
  defaults), and `GET /api/settings` reports it with `source` `env` | `global` |
  `default`. `/api/models` remains and is what the Settings tab calls for this key.
- A **project override** is a partial policy: per map (`harness`,
  `implementation`, `trivial`, `hard_harness`, `hard_implementation`) and per
  engine, a chain replaces the global chain for that engine; anything not named is
  inherited. It is stored in `settings` and validated with the same rules.
- The effective policy for a task is resolved at claim for the task's project and
  sent in the claim payload (`model_policy`, `model_policy_revision`). The channel
  uses it instead of the host file and copies it to the workspace, so
  `argus-delegate` and the attempt evidence see the project's revision. The revision
  is computed over the effective (merged) policy.
- `ARGUS_MODEL_POLICY` still wins over everything, overrides included, and the
  response says so: it is the operator lock.
- Task pins ([task models](task-models.spec.md)) still beat any chain.

## 3.5 Independent code review

Key `review.independent`, scopes `global` and `project`, value:

```json
{ "mode": "off" | "different_model" | "different_provider", "max_rounds": 3 }
```

Default `{"mode": "off", "max_rounds": 3}`. Dashboard label: **Require review by a
different model**, with `different_provider` shown as "…from a different provider".

- **Applies to** tasks that deliver code: completion carries a PR or commit
  reference ([task delivery](task-delivery.spec.md) 3.1). Other tasks (research,
  answers) skip it, with a `review.independent_skipped {reason: no_delivery}` event.
- **Authors** of a task are every model recorded in its
  [model observability](model-observability.spec.md) executions, harness and
  delegate, across all attempts: `observed_model` where present and
  `configured_model` too, because an unknown observation must not make two runs
  look different. `different_provider` compares the execution's provider instead.
- **Gate.** On completion with the mode on, the task moves to `in_review` and Argus
  creates a child task of kind `code_review` in the same project, `prompt_trust:
  orchestrator`, read-only, with input `{task_id, pr, head_sha}`. Its harness and
  executor are pinned ([task models](task-models.spec.md)) to the first model in
  the project's effective policy chains that is not an author (or, for
  `different_provider`, whose engine differs), among engines with a usable account.
- **Verdict.** The review task's result data is `{verdict: approve |
  request_changes, findings: [...]}` with `head_sha`. `approve` for the current
  head passes the gate; the task then goes through its own `review` gate if it has
  one (human/orchestrator decision), else `succeeded`. `request_changes` returns
  the task to `ready` with the findings appended to the spec under **Review
  findings**, without counting a failure, like a review rejection today.
- **Proof, not trust.** The gate passes only if the review task's own executions
  show no author model (or provider). Otherwise the verdict is void, recorded as
  `review.independent_void {reason: same_model}`, and a new reviewer is chosen.
- **No silent skip.** If no eligible reviewer model has a usable account, the task
  stays `in_review` with `block_reason: no_independent_reviewer` and appears in the
  inbox. After `max_rounds` of `request_changes`, a `review` decision for a human
  is opened instead of another round.
- **Timing.** The mode is read when the task completes. Turning it on does not
  reopen succeeded tasks; turning it off lets a waiting gate pass on the next
  completion or a manager's `accept`.

# 4. Open Questions

- Should orchestrators be allowed to write project overrides? Phase 1 says admin
  only, because `review.independent` is a guardrail an orchestrator should not be
  able to weaken. A split (orchestrators may tighten, not loosen) is possible later.
- Comparing models by ID treats `claude-sonnet-5-5` and `claude-opus-5-5` as
  different. Is that independent enough, or should `different_model` mean a
  different family? `different_provider` exists for the strict case.
- Should the PR's agentic review ([gitea helper](gitea-helper.spec.md) 3.4) count
  as an independent review when its model is known? Needs a model on that comment.
- Candidates for later keys: default task `review`, `max_attempts`, lease TTL,
  `ARGUS_MAX_TASKS_PER_AGENT_HOUR`, workspace cleanup. [Routines](routines.spec.md)
  adds `routines.enabled`.

# 5. Constraints

- SQLite only; one table, written through `Database.write()`, time from `tx.now`.
- Resolution is a pure function of the rows and the registry, so the claim path
  and the dashboard compute the same value.
- The model-policy revision stays identical between the API and
  `model-policy.mjs`; the merged policy is hashed by the same canonical rule.

# 6. Implementation Consequences

- Migration `NNNN_settings.sql`: `settings` table.
- `settings` module: registry, validation, resolution; REST routes and MCP tools.
- Claim payload gains `model_policy` and `model_policy_revision`; the NanoClaw
  channel prefers them over `ARGUS_MODEL_POLICY_PATH`, which stays as a fallback for
  older APIs.
- Kind `code_review` registered with its result schema; completion path checks the
  setting and creates the review child; reviewer selection reads the effective
  policy and account availability.
- Dashboard: Settings tab; the model chain editor moves there from Engines.
- Update [model selection](model-selection.spec.md) 4a and
  [worker lifecycle](worker-lifecycle.spec.md) 3.2 (the `in_review` path) in the
  same PR as the code.
