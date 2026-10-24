# Task difficulty, executor routing and the hard tier

# 1. Purpose and Scope

Classify the easiest tasks (a copy change, a typo, a translation) as `trivial` and
let them run on the node's own model: the `local` account (Strata,
[local harness](local-harness.spec.md)) with Cursor (`agent`) on `composer-2.5`.
Classify the hardest tasks (architecture, data migration, security, several
services at once) as `hard` and run their Claude harness and executor on Opus.
Scope: the task `difficulty` field (four levels, replacing `complexity`), its
inferred default, the executor chain per level, escalation, the lease ranking for
the engine role, and the model-policy maps that pin the models.

# 2. Non-Goals

- A hard restriction. `difficulty` only reorders accounts; it never makes a task
  wait for, or fail without, the local model. (Restricting a task to providers is
  `account_providers`, a separate and independent mechanism.)
- More than four levels, or a score. Anything not clearly trivial, simple or hard
  is `standard`.
- Picking a different engine or executor for `hard` tasks, or a Codex hard model:
  no Codex model is better than the default chain yet, so `hard` only overrides
  Claude by default. A Codex entry can be added later through the policy alone.
- Changing claim behaviour, attempts, quota handling or the cloud order of
  `standard` tasks.

# 3. Decisions Made

- **Field.** `difficulty` is `trivial` | `simple` | `standard` | `hard` (default
  `standard`), stored in `tasks.difficulty` (CHECK-constrained). `simple` is work
  that needs no strong model but is more than a copy change; it routes like
  `trivial` to the cheap executor, but only `trivial` prefers the `local` account.
  `difficulty_source` is `declared` (the creator sent it, or an editor set it) or
  `inferred` (the heuristic, or an escalation). Migration
  `0036_task_difficulty.sql` adds both columns, copies `complexity` and
  `complexity_source` (`explicit` becomes `declared`, `heuristic` becomes
  `inferred`) and drops the old columns; one concept, not two in parallel.
- **Legacy `complexity`.** Responses still carry `complexity` (`trivial` |
  `standard` | `hard`) as a read-only computed field (`simple` reads `trivial`),
  and `create_task` still accepts `complexity` as a deprecated alias of
  `difficulty`, so existing orchestrators and the NanoClaw JS keep working.
- **Set on create, editable.** `POST /tasks` and MCP `create_task` take
  `difficulty` (and `difficulty_source`, so an orchestrator can mark a guess
  `inferred`). It is editable under the [task edit](task-edit.spec.md) rules
  (only while `awaiting_approval`, `pending` or `ready`); editing sets the source
  to `declared`. It cannot be cleared.
- **Executor per level.** `POST /api/engines/route` with a `difficulty` also picks
  the executor and model: the first executor of the level's chain whose
  *executor layer* is usable ([engines](engines.spec.md)), with every one passed
  over named in `executor_reason`. Defaults: `trivial` and `simple` use
  `agent, claude, codex`; `standard` and `hard` use `claude, codex`.
  `ARGUS_DIFFICULTY_EXECUTORS` (JSON `{"standard": ["codex", "claude"]}`, validated
  at startup) overrides a level. The model is the first of the model policy's
  `trivial` map for `trivial`/`simple`, `hard_implementation` for `hard`, else
  `implementation`; none means the CLI default (`auto` for Cursor). The decision
  and the `engine.routed` event record `difficulty`, `executor_model` and
  `executor_reason`; `task_attempts.definition` snapshots the difficulty.
- **Escalation.** A failure with reason `context_overflow` or
  `verification_failed`, or a review rejection, raises the difficulty one level
  (stopping at `hard`), sets the source to `inferred` and records
  `difficulty_escalated {from, to, reason}` in the event. Other failures and quota
  handling are unchanged.
- **Heuristic fallback.** Only when the creator sends no `difficulty`,
  `argus_core.difficulty.classify(title, spec)` decides. It returns `trivial` only
  when the title or spec carries a copy signal (copy, typo, wording,
  translation, docs-only), no code-risk signal (migration,
  schema, API, endpoint, security, auth, credential, test, refactor, bug, deploy,
  …) and the spec is at most 1500 characters. It returns `hard` only when the
  title and spec carry at least two distinct hard signals among architecture
  (redesign, re-architect), data migration (backfill, schema migration), security
  (vulnerability, threat model) and several services (multi-service,
  cross-service, distributed); one signal alone is `standard`, since Opus is
  the expensive tier. Anything else is `standard`. A declared value always wins,
  including a declared `standard`; `hard` is meant to be set explicitly by the
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
  `account_providers` narrows the candidate providers first and `difficulty` only
  reorders what remains: a task restricted to `["anthropic"]` stays on it whatever
  its difficulty.

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
- Migrations `0032_task_complexity.sql`, `0033_task_complexity_hard.sql` and
  `0036_task_difficulty.sql` (SQLite ≥ 3.35 for `DROP COLUMN`); all writes go through
  `Database.write()`.

# 6. Implementation Consequences

- `packages/core`: `Difficulty`, `DifficultySource`, `escalated`, `classify`, `fallback_tier(provider, prefer_local)`,
  `TaskCreate`/`Task`/`TaskChanges` fields, the model-policy `trivial`,
  `hard_harness` and `hard_implementation` maps.
- `apps/api`: migration, create/edit, `accounts/service.py` ranking, model policy
  validation and revision, MCP `create_task`, OpenAPI.
- `integrations/nanoclaw`: `model-policy.mjs`, defaults and fixture, `argus.ts`
  workspace context (`complexity`, derived), `argus-delegate`, harness instructions.
- Dashboard: difficulty select on the task form, badge and source on the detail, one row per engine layer, API client.
