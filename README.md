# Argus

Control plane for a fleet of agents. Argus owns shared state (tasks, agents,
assignments), exposes it to an orchestrator agent and to worker agents through an
API, gives humans a dashboard for visibility, and deploys additional worker
instances (containers) on request from the orchestrator or a user.

**Lightweight and single-node first.** Argus is one process with one SQLite
database (continuously backed up by Litestream) and no required external
services: `docker run -v argus-data:/data argus` is a full deployment. The fleet
it manages can live on remote Docker hosts or Kubernetes; Argus itself stays on
one node. See [`docs/specs/single-node.spec.md`](docs/specs/single-node.spec.md).

## Layout

- `apps/api/` — FastAPI control-plane API (Python, `argus_api`).
- `apps/dashboard/` — Operator dashboard (Vite + React + TypeScript), served by
  the API process in production.
- `packages/core/` — Shared domain types (Python, `argus_core`).
- `docs/` — Specs and drafts. Spec first: nothing is built before it is written
  down in `docs/specs/` (see [`docs/README.md`](docs/README.md)).
- `deploy/` — Dockerfiles. `.gitea/` — CI.

## Development

Requires `uv`, [mise](https://mise.jdx.dev) (pins Python, Node, pnpm, uv, gitleaks; `mise install`). Docker only for the `docker` target and image builds.

```bash
make setup        # uv sync + pnpm install
make hooks        # enable the pre-push hook
make dev-api      # API on :8000 under /api (state in ./data, override with ARGUS_DATA_DIR)
make dev-dashboard # http://localhost:3000, proxies /api to :8000
make check        # lint + typecheck + test (what CI runs)
```

## Running the image

```bash
docker build -f deploy/Dockerfile.api -t argus .
docker run -p 8000:8000 -v argus-data:/data argus   # dashboard at http://localhost:8000
# First start prints the admin token and saves it to /data/admin-token;
# or preset one with -e ARGUS_ADMIN_TOKEN=<32+ chars>.
# Provider accounts: mount an age key so Argus can read/write its SOPS file:
#   -v ./age-key.txt:/keys/age.txt:ro -e SOPS_AGE_KEY_FILE=/keys/age.txt \
#   -e SOPS_AGE_RECIPIENTS=age1...   (age-keygen -o age-key.txt)
# Optional continuous backup: add -e LITESTREAM_REPLICA_URL=s3://bucket/argus
# (plus LITESTREAM_ACCESS_KEY_ID / LITESTREAM_SECRET_ACCESS_KEY)
```

