# Task complexity, local routing and the hard tier

# 1. Purpose and Scope

Classify the easiest tasks (a copy change, a typo, a translation) as `trivial` and
let them run on the node's own model: the `local` account (Strata,
[local harness](local-harness.spec.md)) with Cursor (`agent`) on `composer-2.5`.
Classify the hardest tasks (architecture, data migration, security, several
services at once) as `hard` and run their Claude harness and executor on Opus.
Scope: the task `complexity` field, its heuristic default, the lease ranking for
the engine role, and the model-policy maps that pin the models.

# 2. Non-Goals

- A hard restriction. `complexity` only reorders accounts; it never makes a task
  wait for, or fail without, the local model. (Restricting a task to providers is
  `account_providers`, a separate and independent mechanism.)
- More than three levels, or a score. Anything not clearly trivial or hard is
  `standard`.
- Picking a different engine or executor for `hard` tasks, or a Codex hard model:
  no Codex model is better than the default chain yet, so `hard` only overrides
  Claude by default. A Codex entry can be added later through the policy alone.
- Changing claim behaviour, attempts, quota handling or the cloud order of
  `standard` tasks.

# 3. Decisions Made

- **Field.** `complexity` is `trivial` | `standard` | `hard` (default `standard`),
  stored in `tasks.complexity` (CHECK-constrained; rows that predate it are
  `standard`). Migration `0033_task_complexity_hard.sql` widens the CHECK: SQLite
  cannot alter a CHECK, so it adds a new column with the wider CHECK, copies the
  values, drops the old column and renames the new one (no table rebuild; the
  column is referenced by no index, view or trigger).
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
  …) and the spec is at most 1500 characters. It returns `hard` only when the
  title and spec carry at least two distinct hard signals among architecture
  (arquitetura, redesign), data migration (migração de dados, backfill), security
  (segurança, vulnerability, threat model) and several services (multi-service,
  vários serviços, cross-service); one signal alone is `standard`, since Opus is
  the expensive tier. Anything else is `standard`. An explicit value always wins,
  including an explicit `standard`; `hard` is meant to be set explicitly by the
  orchestrator (REST, MCP `create_task`, task edit).
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
- **Hard models.** The policy has two optional maps, `hard_harness` and
  `hard_implementation` (engine → ordered nonempty chain, the same shape as the
  others). Default: `{"claude": ["claude-opus-5-5"]}` in both. For a `hard` task
  the channel selects the harness model from `hard_harness` and `argus-delegate`
  selects the delegated model from `hard_implementation`, each only when the map
  has a chain for that engine; otherwise the normal `harness` / `implementation`
  chain applies, so Codex keeps `gpt-6-luna` / `gpt-6.1-sol`. Adding
  `"codex": [...]` to either map is a policy edit (`PUT /api/models`), no code
  change. `policy_map` names the map used. On a `local` lease a hard task whose
  executor has no hard chain falls back to the `trivial` map as any local lease
  does. Flat maps rather than one nested `hard` object keep one policy shape
  (scope → engine → chain) for the API, `model-policy.mjs` and the Engines view.
  A policy without these maps keeps its revision.
- **Executor.** For a `trivial` task the harness prefers the Cursor executor, as it
  does on local leases, and falls back to the others only if Cursor is unavailable.
- **Composition with `account_providers`.** When both exist, the task's
  `account_providers` narrows the candidate providers first and `complexity` only
  reorders what remains: a task restricted to `["anthropic"]` stays on it whatever
  its complexity.

# 4. Open Questions

- Whether the heuristic should be replaced by a model-based classifier; today it
  is deliberately narrow so a false `trivial` is rare.
- The intended baseline is harness Haiku, executor Sonnet, Opus only for `hard`;
  the default `harness.claude` chain is not changed here (it is policy data).
- Whether `hard` should also avoid the local model entirely; today it only keeps
  the default cloud-first order.
- Whether a trivial task that failed review on the local model should return to
  `standard` automatically.

# 5. Constraints

- Single-node spec; no new service. The heuristic is a pure function, no I/O.
- Migrations `0032_task_complexity.sql` and `0033_task_complexity_hard.sql`
  (SQLite ≥ 3.35 for `DROP COLUMN`); all writes go through
  `Database.write()`.

# 6. Implementation Consequences

- `packages/core`: `Complexity`, `classify`, `fallback_tier(provider, prefer_local)`,
  `TaskCreate`/`Task`/`TaskChanges` fields, the model-policy `trivial`,
  `hard_harness` and `hard_implementation` maps.
- `apps/api`: migration, create/edit, `accounts/service.py` ranking, model policy
  validation and revision, MCP `create_task`, OpenAPI.
- `integrations/nanoclaw`: `model-policy.mjs`, defaults and fixture, `argus.ts`
  workspace context (`complexity`), `argus-delegate`, harness instructions.
- Dashboard: complexity select on the task form, badge on the detail, API client.
