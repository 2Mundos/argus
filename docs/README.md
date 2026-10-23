# Argus — Documentation Index

Everything the team writes down lives here, versioned next to the code it
describes. **Every document under `docs/` must be linked from this page**; CI
fails otherwise (`scripts/validate_docs.py`).

## Conventions

- `docs/specs/topic.spec.md` — **living** documentation, no date. Describes
  current intended behaviour and is updated in the same PR that changes it.
  Follows [`templates/spec.md`](templates/spec.md).
- Everything else is a **point-in-time** record named
  `YYYY-MM-DD.name.type.md` with `type` in `draft`, `research`, `ref`.

| Directory | Holds |
| --- | --- |
| [`specs/`](specs) | Finalized, living specifications |
| [`drafts/`](drafts) | Work in progress, not yet a spec |
| [`research/`](research) | Investigations and comparisons |
| [`runbooks/`](runbooks) | Operational procedures |
| [`releases/`](releases) | Release notes, one per version |

## Core principle

Argus starts **lightweight and single-node**: one process, one SQLite database
(replicated by Litestream), no required external services. Read
[the single-node spec](specs/single-node.spec.md) first; every other spec and
every new dependency is judged against it.

## Index

- [Spec template](templates/spec.md)

### Specs

- [Lightweight single-node](specs/single-node.spec.md) — the core principle:
  one process, SQLite + Litestream, no required services
- [Worker lifecycle and task coordination](specs/worker-lifecycle.spec.md) —
  tasks, leases, agents, provisioning, accounts, decisions, outputs
- [Operations](specs/operations.spec.md) — health, readiness, Prometheus metrics,
  and the System view
- [Worker launch and node capabilities](specs/worker-launch.spec.md) —
  request a worker from the dashboard or MCP; Argus reports what the node can run
- [Engines, executors and the local model](specs/engines.spec.md) —
  who runs the worker loop vs. who gets delegated work; real readiness; local routing
- [Local Strata harness](specs/local-harness.spec.md) — NanoClaw failover to the
  node's Anthropic-compatible model when cloud accounts are unavailable
- [Global account selection](specs/account-selection.spec.md) — leases rank every
  usable account by priority and availability; no pool or worker provider lists.
- [Codex accounts](specs/codex-accounts.spec.md) — import or generate a ChatGPT
  login as an Argus account; engine selection and fallback between engines
- [Claude accounts](specs/claude-accounts.spec.md) — import, generate with a
  dedicated login, or renew an Anthropic credential as an Argus account
- [Cursor accounts](specs/cursor-accounts.spec.md) — import a login or API key
  for the delegated Cursor CLI; executor lease and OneCLI path-scoped injection
- [Custom accounts](specs/custom-accounts.spec.md) — an OpenAI-compatible endpoint
  (OpenRouter first) as an account: model and routing per role, probe, 402/429 failover
- [Custom harness](specs/custom-harness.spec.md) — NanoClaw Claude Code on a custom
  engine account via a loopback Anthropic→OpenAI bridge; executor stays independent
- [Knowledge](specs/knowledge.spec.md) — projects and a versioned wiki shared by
  agents and people, delivered with every claim
- [Home](specs/home.spec.md) — the default dashboard tab: what needs attention,
  complete totals for every core state, recent activity, and health
- [Fleet health](specs/fleet-health.spec.md) — the Frota tab: pool liveness, queue
  and run time, success rate, failures by reason, with `sem dados` when unknown
  Screenshot: [2026-10-20.fleet-health.screenshot.png](evidence/2026-10-20.fleet-health.screenshot.png)
- [Live work](specs/live-work.spec.md) — what an agent is doing now: task activity
  feed, agent drawer with its current work and container output
- [Progress milestones](specs/progress-milestones.spec.md) — structured progress
  signals (commit, PR, CI, gate) on task cards and a no-signal indicator
- [Task live log](specs/task-live-log.spec.md) — a redacted ring of an attempt's
  output, streamed as `task.log`; the "Ao vivo" panel and MCP `get_task_log`
- [Account usage](specs/account-usage.spec.md) — tokens spent and quota left per
  provider account: reported usage, Codex quota probe, Cursor pools, charts
- [Cost view](specs/cost-view.spec.md) — the Custo tab: tokens and estimated cost
  per project and account over 24h/7d/30d, price table, unpriced models
- [Model observability](specs/model-observability.spec.md) — which model ran in the
  harness and delegate layers, with evidence, and the routing/fallback timeline
- [MCP operations](specs/mcp-operations.spec.md) — inbox and fleet journeys over
  MCP with compact, paged responses; REST/MCP coverage audit
- [Worker read scope](specs/worker-read-scope.spec.md) — workers may only read
  tasks/artifacts they have leased and knowledge/events for those projects;
  orchestrators and admins stay global
- [Task archive](specs/task-archive.spec.md) — archive terminal tasks out of lists
  and totals; reversible, never deletes
- [Task dependencies](specs/task-dependencies.spec.md) — edit a waiting task's
  dependencies with a cycle check; stuck dependents surface in the inbox
- [Task edit](specs/task-edit.spec.md) — edit a task's title, spec, criteria, priority,
  capabilities, pins and dependencies with one rule set for REST, MCP and the dashboard
- [Releases](specs/releases.spec.md) — `make release`, version inference, notes
  that say whether an upgrade can be rolled back, the tagged image
- [Task continuation](specs/task-continuation.spec.md) — one typed contract per task:
  phase, artifacts with provenance, checklist evidence, effective decisions, gaps;
  contract first, narrative as history
- [Prompt-data isolation](specs/prompt-data-isolation.spec.md) — task spec,
  acceptance, and knowledge bodies as length-framed untrusted DATA; immutable
  `prompt_trust`; worker child tasks await approval
- [Codex worker sandbox profile (candidate)](specs/codex-sandbox-profile.spec.md) —
  default-on AppArmor/seccomp candidates so Codex's Bubblewrap can run in a
  worker (`off` escape hatch); [provenance](../integrations/nanoclaw/security/PROVENANCE.md)
  and [runbook](../integrations/nanoclaw/security/RUNBOOK.md)
- [Security hardening (2026-10-20 medium/low)](specs/security-hardening.spec.md) —
  response headers and CSP, docs off by default, sessionStorage tokens, pod and
  container hardening, OneCLI URL rules, task/artifact caps, agent token expiry

### Drafts

- [Knowledge](drafts/2026-10-14.knowledge.draft.md) — superseded by the spec
  above; kept as a record
- [Worker lifecycle and task coordination](drafts/2026-10-14.worker-lifecycle.draft.md) —
  superseded by the spec above; kept as a record

### Runbooks

- [NanoClaw worker template: setting up a node](runbooks/2026-10-16.nanoclaw-worker-setup.ref.md) —
  user-owned artifacts, the isolation runtime, environment

### Research

- [NanoClaw integration spike](research/2026-10-14.nanoclaw-integration.research.md) —
  channels, credentials, drivers

- [Preferred models](specs/model-selection.spec.md)
- [Task difficulty, executor routing and escalation](specs/task-difficulty.spec.md) — trivial tasks prefer
  the local Strata account and Cursor `composer-2.5`; the executor chain and escalation follow the level.
- [Task models](specs/task-models.spec.md) — pin a queued task's harness engine/model
  and executor/model; narrows its leases, no fallback chain
- [Model policy operation](runbooks/2026-10-17.model-selection.ref.md)
- [Project agent images](runbooks/2026-10-17.project-agent-images.ref.md) —
  base × tools × project layers, building and pointing a pool at one
- [Validating Claude and Codex as executors](runbooks/2026-10-21.executor-layer-validation.ref.md) —
  procedure, not yet run
- [Cursor executor fleet smoke](runbooks/2026-10-18.cursor-fleet-smoke.ref.md) —
  completed parent verification of delegated `composer-2.5` (2026-10-18)

### Releases

One document per released version, in [`releases/`](releases), written by
`make release`. Indexed as a series rather than file by file: the filenames are
`YYYY-MM-DD.vX-Y-Z.ref.md` and sort chronologically.
