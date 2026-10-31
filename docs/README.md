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
| [`reports/`](reports) | Point-in-time review and QA receipts |
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
  and the System widgets
- [Worker launch and node capabilities](specs/worker-launch.spec.md) —
  request a worker from the dashboard or MCP; Argus reports what the node can run
- [Project runtime images](specs/project-runtime.spec.md) — per-project agent
  images, build cache, probes, disposable PostgreSQL/Valkey, and worker contract
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
- [Copilot accounts](specs/copilot-accounts.spec.md) — import a GitHub Copilot token for
  the delegated Copilot CLI; executor lease, sentinel env and OneCLI injection
- [Custom accounts](specs/custom-accounts.spec.md) — an OpenAI-compatible endpoint
  (OpenRouter first) as an account: model and routing per role, probe, 402/429 failover
- [Custom harness](specs/custom-harness.spec.md) — NanoClaw Claude Code on a custom
  engine account via a loopback Anthropic→OpenAI bridge; executor stays independent
- [Knowledge](specs/knowledge.spec.md) — projects and a versioned wiki shared by
  agents and people, delivered with every claim
- [Home](specs/home.spec.md) — the default dashboard tab: what needs attention,
  complete totals for every core state, recent activity, and health
- [Agent hygiene](specs/agent-hygiene.spec.md) — lost/gone identities by pool and
  heartbeat age, bounded opt-in archiving, and the lost-holding-claims and
  lost-spike alerts
- [Failure classes](specs/failure-classes.spec.md) — one class per failure reason
  picks the retry (block, backoff, requeue, hand to a human), and a channel
  preflight blocks environment problems before any model work
- [Fleet health](specs/fleet-health.spec.md) — the Fleet tab: pool liveness, queue
  and run time, success rate, failures by reason, with `no data` when unknown
  Screenshot: [2026-10-20.fleet-health.screenshot.png](evidence/2026-10-20.fleet-health.screenshot.png)
- [OneCLI cleanup](specs/onecli-cleanup.spec.md) — opt-in, dry-run-first deletion
  of the OneCLI agent and secrets of terminated NanoClaw instances
- [Workspace cleanup](specs/workspace-cleanup.spec.md) — opt-in, dry-run-first
  deletion of terminated NanoClaw instance directories after their unpublished git
  work is archived and verified
- [Task workspace](specs/task-workspace.spec.md) — a stable git workspace per
  `repository:write` task, keyed by task id, created and checked read-only
  before the model, blocking with a typed `env_workspace_*` reason
- [Task checkpoint](specs/task-checkpoint.spec.md) — hash-verified, node-local
  preservation of a task's unpublished work and its restore by the next attempt,
  or a typed `env_checkpoint_*` block
- [Live work](specs/live-work.spec.md) — what an agent is doing now: task activity
  feed, agent drawer with its current work and container output, and a fixed
  live status bar (expandable recent-event feed)
- [Gitea helper for workers](specs/gitea-helper.spec.md) — `argus-gitea-api`
  command matrix, exit codes, security model, and the review-findings gate
- [GitHub repositories](specs/github-repositories.spec.md) — `github` repository
  accounts, OneCLI injection for github.com, and `argus-github-api`
- [Progress milestones](specs/progress-milestones.spec.md) — structured progress
  signals (commit, PR, CI, gate) on task cards and a no-signal indicator
- [Stall visibility](specs/stall-visibility.spec.md) — fail a vanished session
  (`session_lost`), flag quiet claimed tasks in the inbox and worker rail, and
  explain late lease/agent expiry and the likely stall cause
- [Task live log](specs/task-live-log.spec.md) — a redacted ring of an attempt's
  output, streamed as `task.log`; the Live panel and MCP `get_task_log`
- [Task changes](specs/task-changes.spec.md) — a bounded, redacted diff of the task
  workspace, computed in the container and shown in a Changes panel beside Live
- [Task cost and budget](specs/task-cost.spec.md) — estimated spend per task and
  attempt, live; an optional `budget_usd` steers the attempt to wrap up and blocks new attempts
- [Task steering](specs/task-steering.spec.md) — a manager sends a message to the
  attempt in flight; delivered on heartbeat, injected into the NanoClaw session as a
  framed operator instruction; expires with the attempt, or ends `unknown` when it
  was dispatched and never acknowledged
- [Account usage](specs/account-usage.spec.md) — tokens spent and quota left per
  provider account: reported usage, Codex quota probe, Cursor pools, charts
- [Cost view](specs/cost-view.spec.md) — the Cost tab: tokens and estimated cost
  per project and account over 24h/7d/30d, price table, unpriced models
- [Model observability](specs/model-observability.spec.md) — which model ran in the
  harness and delegate layers, with evidence, and the routing/fallback timeline
- [MCP operations](specs/mcp-operations.spec.md) — inbox and fleet journeys over
  MCP with compact, paged responses; REST/MCP coverage audit
- [Task inputs](specs/task-inputs.spec.md) — byte-exact upload of shared
  attachments, the inputs view and receipts, materialization by the NanoClaw
  channel, required inputs and `env_input_*` blocks
- [Task resumability preflight](specs/task-resumability-preflight.spec.md) —
  read-only assessment of task/attempt/lease state, WIP and attachment
  availability before answer/requeue/release; metadata and receipts are not
  current proof
- [Worker read scope](specs/worker-read-scope.spec.md) — workers may only read
  tasks/artifacts they have leased and knowledge/events for those projects;
  orchestrators and admins stay global
- [Task archive](specs/task-archive.spec.md) — archive terminal tasks out of lists
  and totals; reversible, never deletes
- [Task dependencies](specs/task-dependencies.spec.md) — edit a waiting task's
  dependencies with a cycle check; stuck dependents surface in the inbox
- [Task edit](specs/task-edit.spec.md) — edit a task's title, spec, criteria, priority,
  capabilities, pins and dependencies with one rule set for REST, MCP and the dashboard
- [Task drafts](specs/task-drafts.spec.md) — persisted, never-claimable task drafts
  published in one validated, idempotent transaction (atomic or partial)
- [Pool editor](specs/pool-editor.spec.md) — separate host and NanoClaw agent image fields with verified
  choices, version-checked saves, field-level errors
- [Releases](specs/releases.spec.md) — `make release`, version inference, notes
  that say whether an upgrade can be rolled back, the tagged image
- [Task delivery](specs/task-delivery.spec.md) — read-only PR projection (state,
  exact-head checks/statuses, mergeability, review evidence; checklist and
  authorization separate) on REST, MCP and the dashboard; a merge is flagged for
  review, never accepted; green does not authorize merge.
- [Task discovery and prerequisite evidence](specs/task-discovery.spec.md) —
  bounded task text search with citations, and a read-only view of whether
  dependencies are forge-merged or artifact-verified (distinct from `succeeded`)
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
  container hardening, OneCLI URL rules, task/artifact caps, agent token expiry;
  reporting a vulnerability: [SECURITY.md](../SECURITY.md)

- [Settings](specs/settings.spec.md) — typed global settings with per-project
  overrides; model preferences move there; optional review by a different model
  before a code task succeeds
- [Routines](specs/routines.spec.md) — cron-scheduled task templates per project;
  run findings become deduplicated tasks, approval-gated by default
- [Internal orchestrator](specs/internal-orchestrator.spec.md) — Argus hosts its own
  orchestrator as a system pool; chat with it is the main page; spend cap; skill improvements
- [Onboarding](specs/onboarding.spec.md) — a wizard from a fresh install to an account,
  a first worker and optionally the internal orchestrator; Home becomes Dashboard

### Drafts

- [Task operations and reliability](drafts/2026-10-29.task-operations.draft.md) — task
  detail view, command reliability, finalization, workspace ownership, checkpoints
  and engine events; candidates for investigation and specification
- [Knowledge](drafts/2026-10-14.knowledge.draft.md) — superseded by the spec
  above; kept as a record
- [Worker lifecycle and task coordination](drafts/2026-10-14.worker-lifecycle.draft.md) —
  superseded by the spec above; kept as a record
- [Worker efficiency](drafts/2026-10-24.worker-efficiency.draft.md) — why tasks are
  interrupted and tokens wasted (baseline numbers, root causes), and eight ordered
  proposals: claim reserves the account, owned workspace, one task per session,
  resumable attempts, progress-based liveness, failure classes, single-layer run;
  P1, P3, P6 and P8 shipped, P2/P5 partly (P5 recovery latency now readable on Fleet)
- [Command guard](drafts/2026-10-29.command-guard.draft.md) — a `PreToolUse`
  hook that blocks catastrophic shell commands (root/home deletion, disk wipes,
  repository deletion, fork bombs); accident prevention, not a sandbox
- [Disk guard](drafts/2026-10-29.disk-guard.draft.md) — warn, refuse claims,
  pause session containers, then stop the heaviest writer, in hysteresis bands
  before SQLite's 64 MiB limit
- [Agent resource hygiene](drafts/2026-10-29.agent-resource-hygiene.draft.md) —
  agent commands die before the harness on OOM, CPU counts match the quota,
  OOM kills classified as `env_oom`
- [Quota resume](drafts/2026-10-29.quota-resume.draft.md) — after a quota pause,
  resume the same native conversation under the new account; a slice of
  worker-efficiency P4
- [Quota resume research](research/2026-10-29.quota-resume.research.md) — credential
  swap is a container recreate today; session validity and Codex resume left open;
  spec-update plan folding into P4
- [Account auth health](drafts/2026-10-29.auth-health.draft.md) — credential
  state and last accepted/rejected request per account, kept separate
- [Harness stderr tail](drafts/2026-10-29.harness-stderr-tail.draft.md) — a
  bounded, redacted stderr artifact on every attempt exit, with truncation flags
- [Harness stderr tail research](research/2026-10-29.harness-stderr-tail.research.md) —
  the NanoClaw hook drops container stderr today (only `docker logs` stdout is kept)
- [Bounded recovery](drafts/2026-10-29.bounded-recovery.draft.md) — throttled
  session starts, one recovery per unexpected exit, uncertain work never resent
  (item 3 shipped for steers)
- [Verification boundary](drafts/2026-10-29.verification-boundary.draft.md) — a
  spec subsection stating what was verified, at which level and versions
- [Model-facing evals](drafts/2026-10-31.model-facing-evals.draft.md) — run real
  models against the real MCP tool text, persona and worker prompt; cases from
  real failures, controls, baselines; not in CI
- [Test hygiene checks](drafts/2026-10-31.test-hygiene-lint.draft.md) — a lint
  step that rejects sleeps, polling, wall-clock asserts, retries and unreasoned
  skips or suppressions in tests, and clock reads beside `tx.now`
- [Replay engine](drafts/2026-10-31.replay-engine.draft.md) — a dev-only worker
  that plays back a recorded attempt through the real API, with no model
- [Worktree dev isolation](drafts/2026-10-31.worktree-dev-isolation.draft.md) —
  state and ports per worktree, seeded from a pruned read-only copy of the
  preview database that cannot start workers
- [Changed-only checks](drafts/2026-10-31.changed-only-checks.draft.md) — a
  pre-push run of only the checks the changed files can affect

### Runbooks

- [NanoClaw worker template: setting up a node](runbooks/2026-10-16.nanoclaw-worker-setup.ref.md) —
  user-owned artifacts, the isolation runtime, environment
- [Workspace cleanup](runbooks/2026-10-24.workspace-cleanup.ref.md) — enabling it,
  dry run first, the archive root, restore and reason codes
- [Task inputs smoke](runbooks/2026-10-30.task-inputs-smoke.ref.md) — operator-run
  Docker session check of materialized inputs; not yet run
- [Session quota smoke](runbooks/2026-10-30.session-quota-smoke.ref.md) — operator-run
  Docker check that NanoClaw sessions carry pool `--cpus`/`--memory`; ASSIST
  procedure when no daemon is available; not yet run
- [OOM evidence smoke](runbooks/2026-10-31.oom-smoke.ref.md) — disposable
  `--memory 64m` probe, `OOMKilled`/`ExitCode` via the channel's reader, `docker kill`
  negative control; not yet run

### Reports

- [Session quota recovery (2026-10-30)](reports/2026-10-30.session-quota-recovery.ref.md) —
  commit `64b8d8c` unrecoverable; reimplemented pool→`CONTAINER_*_LIMIT` propagation,
  `session_quota` evidence, Docker ASSIST runbook; live daemon still a blocker
- [Disk guard investigation (2026-10-29)](reports/2026-10-29.disk-guard-investigation.ref.md) —
  who runs the loop, `io.stat` vs `docker ps --size`, cleanup dry run on warn,
  pause vs leases and stall detection; spec-update plan for the disk-guard draft
- [Workspace cleanup exact-head review (task f9a048fa)](reports/2026-10-24.workspace-cleanup-review.ref.md) —
  independent P1–P3 review of the merged cleanup feature; non-git state and
  committed secrets explicitly not total preservation
- [Workspace cleanup QA receipt (task f9a048fa)](reports/2026-10-24.workspace-cleanup-qa.ref.md) —
  commands, exit statuses, restore proof, Biome/`make check`, CI probe, limitations
- [Workspace cleanup delegate review extract](reports/2026-10-24.workspace-cleanup-delegate-review.ref.md) —
  verbatim independent `argus-delegate --read-only` report at baseline SHA `60576ab…`
  (P3-2 later fixed in PR #114; final-head review stored outside git)

### Research

- [NanoClaw integration spike](research/2026-10-14.nanoclaw-integration.research.md) —
  channels, credentials, drivers

- [Task workspace ownership visibility](research/2026-10-29.workspace-ownership.research.md) —
  task detail view: owner, location kind, changes/checkpoint availability (draft §4)
- [Focused task detail view](research/2026-10-29.task-detail-view.research.md) —
  journey map, gaps, stream filtering, recommendations
- [Agent resource hygiene investigation](research/2026-10-29.agent-resource-hygiene.research.md) —
  session container inheritance of pool limits, oom_score_adj and CPU shims, OOM detection, spec-update plan
- [Auth health investigation](research/2026-10-29.auth-health.research.md) —
  derivability of the last-accepted and last-rejected timestamps, Claude's stream auth-failure signal (2.1.296),
  Codex `auth_expired` path, and the spec-update plan
- [Agent shell command guard](research/2026-10-29.command-guard.research.md) —
  Claude/Codex hook registration, Codex pin, failure class, event, setting, rule list
- [Bounded startup and recovery](research/2026-10-29.bounded-recovery.research.md) —
  start throttle location, one-resume rule, unknown outcome audit (draft bounded-recovery)

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
- [Reply fencing activation](runbooks/2026-10-24.reply-fencing-activation.ref.md) —
  containment and manual activation of the cross-task reply fence
- [Cursor executor fleet smoke](runbooks/2026-10-18.cursor-fleet-smoke.ref.md) —
  completed parent verification of delegated `composer-2.5` (2026-10-18)

### Releases

One document per released version, in [`releases/`](releases), written by
`make release`. Indexed as a series rather than file by file: the filenames are
`YYYY-MM-DD.vX-Y-Z.ref.md` and sort chronologically.
