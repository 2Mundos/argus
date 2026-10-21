# Task complexity and local routing

# 1. Purpose and Scope

Classify the easiest tasks (a copy change, a typo, a translation) as `trivial` and
let them run on the node's own model: the `local` account (Strata,
[local harness](local-harness.spec.md)) with Cursor (`agent`) on `composer-2.5`.
Scope: the task `complexity` field, its heuristic default, the lease ranking for
the engine role, and the model-policy map that pins the model.

# 2. Non-Goals

- A hard restriction. `complexity` only reorders accounts; it never makes a task
  wait for, or fail without, the local model. (Restricting a task to providers is
  `account_providers`, a separate and independent mechanism.)
- More than two levels, or a score. Anything not clearly trivial is `standard`.
- Changing claim behaviour, attempts, quota handling or the cloud order of
  `standard` tasks.

# 3. Decisions Made

- **Field.** `complexity` is `trivial` | `standard` (default `standard`), stored in
  `tasks.complexity` (CHECK-constrained; rows that predate it are `standard`).
  `complexity_source` is `explicit` (the creator sent it, or an editor set it) or
  `heuristic` (the creator did not; also the value for rows that predate the
  field). It is a second column of the same migration rather than an event field,
  so read models and the dashboard can show it without replaying events.
- **Set on create, editable.** `POST /tasks` and MCP `create_task` take
  `complexity`. It is editable under the [task edit](task-edit.spec.md) rules
  (only while `awaiting_approval`, `pending` or `ready`); editing sets the source
  to `explicit`. It cannot be cleared.
- **Heuristic fallback.** Only when the creator sends no `complexity`,
  `argus_core.complexity.classify(title, spec)` decides. It returns `trivial` only
  when the title or spec carries a copy signal (copy, text, typo, wording,
  translation, docs-only; English and pt-BR), no code-risk signal (migration,
  schema, API, endpoint, security, auth, credential, test, refactor, bug, deploy,
  …) and the spec is at most 1500 characters. Anything else is `standard`. An
  explicit value always wins, including an explicit `standard`.
- **Routing is a soft preference.** For the `engine` role the lease ranking of a
  `trivial` task puts `local` first (tier 0) and every cloud account after it
  (tier 1); then priority, load and the deterministic ties as before. `standard`
  tasks keep cloud before local. The ranking applies to every lease path of the
  claimed task: `lease`, the failover after `exhausted` and after a quota
  `fail`.
- **Immediate fallback.** If the local account cannot serve (leased to
  `max_leases`, probe failing so disabled, or no `local` runtime) the same lease
  call takes the best cloud account. It never waits, blocks, or spends an extra
  attempt: the local model is a single GPU slot (`max_leases` 1) and must not
  become a queue. Claim is unchanged for trivial tasks; there is no claim-time skip.
  Lease checks (task id, attempt, live claim) run before the ranking, unchanged.
- **Model pinning.** The model policy ([model selection](model-selection.spec.md))
  has an optional `trivial` map (engine → ordered nonempty chain), default
  `{"agent": ["composer-2.5"]}`. `argus-delegate` uses it instead of
  `implementation` when the task is `trivial` or the engine lease's account
  provider is `local`. `--model` and a custom account's model keep precedence.
  `model_source` is `policy` and `policy_map` names the map used.
- **Executor.** For a `trivial` task the harness prefers the Cursor executor, as it
  does on local leases, and falls back to the others only if Cursor is unavailable.
- **Composition with `account_providers`.** When both exist, the task's
  `account_providers` narrows the candidate providers first and `complexity` only
  reorders what remains: a task restricted to `["anthropic"]` stays on it whatever
  its complexity.

# 4. Open Questions

- Whether the heuristic should be replaced by a model-based classifier; today it
  is deliberately narrow so a false `trivial` is rare.
- Whether a trivial task that failed review on the local model should return to
  `standard` automatically.

# 5. Constraints

- Single-node spec; no new service. The heuristic is a pure function, no I/O.
- One migration (`0031_task_complexity.sql`); all writes go through
  `Database.write()`.

# 6. Implementation Consequences

- `packages/core`: `Complexity`, `classify`, `fallback_tier(provider, prefer_local)`,
  `TaskCreate`/`Task`/`TaskChanges` fields, the model-policy `trivial` map.
- `apps/api`: migration, create/edit, `accounts/service.py` ranking, model policy
  validation and revision, MCP `create_task`, OpenAPI.
- `integrations/nanoclaw`: `model-policy.mjs`, defaults and fixture, `argus.ts`
  workspace context (`complexity`), `argus-delegate`, harness instructions.
- Dashboard: complexity select on the task form, badge on the detail, API client.
