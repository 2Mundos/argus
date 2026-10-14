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

### Drafts

- [Knowledge](drafts/2026-10-14.knowledge.draft.md) — a shared, versioned wiki
  for agents and humans (proposal, open questions)
- [Worker lifecycle and task coordination](drafts/2026-10-14.worker-lifecycle.draft.md) —
  superseded by the spec above; kept as a record
