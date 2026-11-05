---
status: implemented
created: 2026-11-05
updated: 2026-11-05
---

# Worktree dev isolation

# 1. Purpose and Scope

Agents develop Argus in several git worktrees at once, on the machine that also
runs the preview install (`~/work/argus`, `data/argus.db`, `argus-preview` on
:8000). Dev servers must not collide with each other or touch the preview data,
and should run on realistic data. `scripts/dev.py` (`make dev-api`,
`make dev-dashboard`, `make seed-dev`) gives each checkout its own state, ports and
pruned copy of real data. Promoted from the 2026-10-31 worktree-dev-isolation draft.

# 2. Non-Goals

- Not sandboxing agents themselves; this covers the developer's dev servers.
- Data never flows back to the preview install, and artifacts are not copied.

# 3. Decisions Made

## 3.1 State per checkout

`make dev-api` uses the checkout's gitignored `.argus/` as its data dir, in any
checkout. An inherited `ARGUS_DATA_DIR` is ignored; only `--data-dir` (make
`DATA_DIR=`) overrides it. The API refuses to start on the preview checkout's
`data/` unless it runs from `~/work/argus` (`refuse_preview_data` in `config.py`).

## 3.2 Ports

API and dashboard ports derive from a hash of the checkout path, API in
8100-8199 and dashboard in 3100-3199, shifting when occupied, printed on one
`dev:` line. The dashboard binds localhost only and proxies its checkout's API.

## 3.3 Seed from real data

`make seed-dev`, and the first `dev-api` on an empty data dir, take a read-only
SQLite backup of `~/work/argus/data/argus.db` (overridden only by `PREVIEW_DB=`)
and prune the copy so it cannot start workers, spend quota or reach nodes:

- keeps all projects and the 200 most recent non-queued tasks (`--keep-tasks N`)
  with their attempts, logs and events (at most 10 000 events);
- drops queued tasks (`pending`, `ready`, `claimed`), routines, open leases and
  claims, agent tokens, auth sessions, account credentials, artifact rows and
  open decisions not about a kept task; marks agents gone and sets every pool's
  desired and maximum replicas to zero;
- prunes by the copy's own schema (`PRAGMA foreign_key_list` and `task_id`
  columns), and every table must be listed in `dev.py` as kept, dropped or account
  data: an unlisted table fails the seed, and a source newer than the checkout's
  migrations is refused;
- a source over 512 MiB warns and starts empty; `--no-seed` starts empty;
  `seed-dev` replaces an existing dev database.

## 3.4 Credentials only on request

Accounts and secrets are copied only with `ACCOUNTS=1` (`--accounts`), and only
into the checkout's `.argus/`.

# 4. Open Questions

- None open; artifacts stay uncopied unless a flow needs a second artifact root.

# 5. Constraints

- The preview database is only read (SQLite backup API); never written or linked.
- `dev.py` uses the standard library plus `argus_api`'s migration loader.

# 6. Implementation Consequences

- `scripts/dev.py`, Makefile targets `dev-api`, `dev-dashboard`, `seed-dev`;
  `refuse_preview_data` in `apps/api/src/argus_api/config.py`.
- Tests: `scripts/test_dev.py`.

## Verification boundary

- Commands: `uv run pytest scripts/test_dev.py`.
- Verified: ports, data-dir precedence, pruning and the table inventory, refusal
  of newer schemas, at unit level against a fixture database; used daily by agents.
- Not verified: behaviour on a preview database over the size limit beyond the warning path.
