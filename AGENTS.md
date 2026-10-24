# Argus — AGENTS.md

Instructions for AI agents working in this repo.

## Stack
- Python 3.12+, `uv` workspace (`apps/api`, `packages/core`), `ruff`, `ty`, `pytest`.
- Toolchain versions pinned in `mise.toml`.
- TypeScript 5 / Node 22, `pnpm` workspace (`apps/dashboard`), Vite + React 19, Biome, Vitest.
- CI: Gitea Actions (`.gitea/workflows/quality.yaml`). Local mirror: `make check`, run on every push by the pre-push hook (`make hooks`).

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
- **English only.** UI copy, docs, specs, comments, identifiers, tests, and
  commits are English. Do not add or leave Portuguese (or other languages) in
  the repo, including classifier synonyms and sample fixtures.
- Never commit secrets (gitleaks runs in CI and pre-push).

## Commands
```bash
make setup   # install everything
make check   # lint + typecheck + test
make format  # autofix
make release ARGS=--dry-run  # preview the next release (docs/specs/releases.spec.md)
```
