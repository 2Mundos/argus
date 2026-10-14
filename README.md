# Argus

Control plane for a fleet of agents. Argus owns shared state (tasks, agents,
assignments), exposes it to an orchestrator agent and to worker agents through an
API, gives humans a dashboard for visibility, and deploys additional worker
instances (containers) on request from the orchestrator or a user.

## Layout

- `apps/api/` — FastAPI control-plane API (Python, `argus_api`).
- `apps/dashboard/` — Operator dashboard (Vite + React + TypeScript).
- `packages/core/` — Shared domain types (Python, `argus_core`).
- `docs/` — Specs and drafts. Spec first: nothing is built before it is written
  down in `docs/specs/` (see [`docs/README.md`](docs/README.md)).
- `deploy/` — Dockerfiles. `.gitea/` — CI.

## Development

Requires `uv`, [mise](https://mise.jdx.dev) (pins Python, Node, pnpm, uv, gitleaks; `mise install`), Docker.

```bash
make setup        # uv sync + pnpm install
make hooks        # enable the pre-push hook
make infra-up     # Postgres
make dev-api      # http://localhost:8000
make dev-dashboard # http://localhost:3000
make check        # lint + typecheck + test (what CI runs)
```
