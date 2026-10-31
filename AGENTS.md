# Argus — AGENTS.md

Instructions for AI agents working in this repo.

## Stack
- Python 3.12+, `uv` workspace (`apps/api`, `packages/core`), `ruff`, `ty`, `pytest`.
- Toolchain versions pinned in `mise.toml`.
- TypeScript 5 / Node 22, `pnpm` workspace (`apps/dashboard`), Vite + React 19, Biome, Vitest.
- CI: GitHub Actions (`.github/workflows/quality.yaml`). Local mirror: `make check`, run on every push by the pre-push hook (`make hooks`).

## Rules
- **Lightweight, single-node first** ([spec](docs/specs/single-node.spec.md)). One
  process, SQLite (WAL, one writer, Litestream backups), local filesystem for
  artifacts, in-process background loops and event bus. Never add a required
  external service (Postgres, Redis, a broker, S3, Elasticsearch, …); optional
  integrations are off by default. Changing this needs a spec PR first.
- **Spec first.** Behaviour changes start in `docs/specs/` (`topic.spec.md`, six-section
  template). Other docs are `YYYY-MM-DD.name.type.md`. Link every doc from `docs/README.md`.
- **Negentropy.** Code is a liability; add only what is needed.
- **Schema changes** are a new `apps/api/src/argus_api/db/migrations/NNNN_name.sql`
  (next number, no gaps). Never edit a migration that has shipped. All writes go
  through `Database.write()`; use `tx.now` for time, never a clock read elsewhere,
  and make no network calls inside a write transaction.
- **Ruff is the formatting authority** for Python, Biome for TS. `make format` fixes both.
- TypeScript is `strict`; no `any` without a written justification.
- The dashboard talks to the API only; never to workers or the database directly.
- **English only.** UI copy, docs, specs, comments, identifiers, tests,
  commits, pull request titles and descriptions, review comments, and issues
  are English, even when the task text is in another language. Do not add or
  leave Portuguese (or other languages) in the repo, including classifier
  synonyms and sample fixtures.
- Never commit secrets (gitleaks runs in CI and pre-push).
- **Living specs are rewritten, not appended to.** When a decision changes, edit
  or delete the text it replaces; history lives in git and dated docs.

## Don't damage the machine you run on
This node also runs the preview install and other agents' worktrees.
- **Stop only what you started.** Kill a PID you recorded when you spawned it, or
  the owner of your port after checking its `/proc/<pid>/cwd` is your worktree.
  Never `pkill -f` or kill by matching a name or path: your own agent's argv
  contains the worktree path, and other agents run the same commands.
- **The preview install is read-only to you.** `~/work/argus/data/argus.db` and
  the `argus-preview` service are live. Read or copy from them; never point a dev
  server at that data, open it read-write, restart the service outside
  `make reload-preview`, or clean it up.
- **Extra checkouts** are `git worktree add`ed next to yours, not in `/tmp`, and
  removed when you are done.

## Cover every surface
A change that works on the path you tried is often missing elsewhere. Before
calling work done, check which of these apply and say so:
- **Entry points.** A capability in the dashboard usually also belongs in the
  REST API and an MCP tool, and routines and the orchestrator reach it through
  those. Keep it in a service function so every transport calls the same code.
- **Engines.** Claude, Codex, Cursor, Copilot, custom and local each need a
  decision, even if it is "not supported".
- **Contracts.** Anything on the wire goes through the API models; regenerate
  with `make openapi` so the dashboard client follows.
- **Way back.** Every way in needs a way out and a way to see it: pause needs
  resume, archive needs restore, block needs unblock.
- **Docs.** Update the spec the change makes inaccurate.

## Tests
- Test what can break: state transitions, leases and retries, failure
  classification, persistence and migrations, parsing of external formats,
  auth and scope checks, races, and the logic a bug fix touched. Skip what types
  already guarantee, copy text, and mock-call mirrors.
- Test at one layer: the lowest one that fails on `main`. Don't repeat the case
  in wrappers or at each call site.
- Extend before adding: add a row or assertion to an existing test when one fits.
- A logic, data or protocol bug gets a failing test first, then the fix. A purely
  visual bug gets before/after screenshots instead of a test.
- No sleeps, polling loops, per-test timeouts, retries or elapsed-time asserts.
  Await the event or future that marks the milestone; fix flaky tests, don't
  retry them. A skip needs a `reason=`.
- Real model runs cost money. Use one only when the change depends on that
  engine's behaviour, and pick its cheapest model.

## Commands
```bash
make setup   # install everything
make check   # lint + typecheck + test
make format  # autofix
make release ARGS=--dry-run  # preview the next release (docs/specs/releases.spec.md)
```
