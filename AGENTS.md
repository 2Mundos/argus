# Argus — AGENTS.md

Instructions for AI agents working in this repo.

## Stack
- Python 3.12+, `uv` workspace (`apps/api`, `packages/core`), `ruff`, `ty`, `pytest`.
- Toolchain versions pinned in `mise.toml`.
- TypeScript 5 / Node 22, `pnpm` workspace (`apps/dashboard`), Vite + React 19, Biome, Vitest.
- CI: Gitea Actions (`.gitea/workflows/quality.yaml`). Local mirror: `make check`.

## Rules
- **Spec first.** Behaviour changes start in `docs/specs/` (`topic.spec.md`, six-section
  template). Other docs are `YYYY-MM-DD.name.type.md`. Link every doc from `docs/README.md`.
- **Negentropy.** Code is a liability; add only what is needed.
- **Ruff is the formatting authority** for Python, Biome for TS. `make format` fixes both.
- TypeScript is `strict`; no `any` without a written justification.
- The dashboard talks to the API only; never to workers or the database directly.
- Never commit secrets (gitleaks runs in CI and pre-push).

## Commands
```bash
make setup   # install everything
make check   # lint + typecheck + test
make format  # autofix
```
