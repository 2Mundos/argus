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
- [Codex accounts](specs/codex-accounts.spec.md) — import or generate a ChatGPT
  login as an Argus account; engine selection and fallback between engines
- [Claude accounts](specs/claude-accounts.spec.md) — import, generate with a
  dedicated login, or renew an Anthropic credential as an Argus account
- [Knowledge](specs/knowledge.spec.md) — projects and a versioned wiki shared by
  agents and people, delivered with every claim
- [Home](specs/home.spec.md) — the default dashboard tab: what needs attention,
  complete totals for every core state, recent activity, and health
- [MCP operations](specs/mcp-operations.spec.md) — inbox and fleet journeys over
  MCP with compact, paged responses; REST/MCP coverage audit

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
