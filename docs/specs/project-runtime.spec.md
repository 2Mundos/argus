# Project runtime images

# 1. Purpose and Scope

Each **project** may declare what its tasks need in the **agent/executor image**
(the NanoClaw session container), distinct from the **host** image that runs the
worker loop. Argus turns that declaration into a derived image on the node's
Docker daemon, probes it as the task uid with session hardening, and only then
lets the worker start model or implementation work.

In scope:

- An **approved catalogue** of component ids (`python`, `psql`, `valkey`, …);
  projects reference ids and optional version pins, never Dockerfile text.
- **Per-project recipes** rendered only by Argus (`render_dockerfile`), built
  on top of the worker's reported **base image** (tools layer), tagged under a
  configurable repository (default `argus-project-runtime`).
- **Build cache and deduplication** keyed by project, recipe digest, Docker host
  id, and `run_as` uid; states `preparing` → `building` → `verifying` → `ready`
  or `failed`, with probe evidence. `ready` never means "built" without probes.
- **Claim gate**: workers that have called `prepare` once are not handed tasks
  whose project image is `failed` or still building on that host/uid/base.
- **Task holds** (`runtime_preparing`, `runtime_failed`) that do not spend
  `failures` or `max_attempts`; preparation/build failures are durable on the
  build row, not retried every claim.
- **Disposable test services** (PostgreSQL, Valkey) per task via
  `argus-test-services` inside the project image; URLs injected through declared
  env templates, segregated data dirs per task.
- **REST** (`/runtime/*`, `/projects/{slug}/runtime`), **MCP** tools, and the
  **NanoClaw** `argus.ts` worker path (`ensureRuntime`, `verifyRuntimeUsed`).
- **Observability**: requirement, `build_key`, selected image ref/id, tool probe
  output, service contract, explicit **gaps** for catalogue entries marked
  `gap` or optional components not provisioned.

Radar is the reference project: `psql`, PostgreSQL server binaries, Valkey
server and CLI, Python ≥ 3.12 via uv, pango stack, pinned `graft`, and declared
gaps for `pnpm`, `sops-age`, `minio`, and `elasticsearch` until provisioned.

# 2. Non-Goals

- Replacing NanoClaw's three-layer manual scripts as the only way to build
  images on a developer laptop; Argus automates the **same layering** for tasks
  dispatched through it, using the worker-reported base ref.
- A central image registry or catalogue service; images are local to the daemon
  that built them.
- Arbitrary `Dockerfile` or shell from task specs, project pages, or worker
  input; only the catalogue recipes run at build time.
- Mounting the Docker socket, `SYS_ADMIN`, or privileged mode inside the agent
  image; builds use a stripped environment (no Argus tokens or provider secrets
  in the build subprocess env).
- Provisioning every dependency a repository might mention by heuristic crawl;
  undeclared needs surface as **gaps**, not silent success.
- MinIO or Elasticsearch lifecycle in this spec (gaps only until a disposable
  lifecycle exists).

# 3. Decisions Made

- **Declaration.** Admins set `RuntimeSpec`: `components[]` (`id`, optional
  `version`, `required`), optional `services[]` (`kind`: `postgres` | `valkey`,
  `env` name → placeholder template), optional `codex_turn_timeout_s`. Invalid
  ids, pins, env names, or templates return `ValidationFailedError` on write.
  Absent row means the project declares nothing (worker runs on base image;
  gate does not apply until a spec exists and the agent has prepared once).
- **Recipe and build key.** `resolve` + `recipe_digest` hash the normalized
  spec, catalogue pins, renderer `SCHEMA` version, helper binary, base image id,
  platform, and `run_as`. Two tasks on the same project/host/uid with the same
  inputs reuse one `runtime_builds` row; a changed base id, recipe, helper, or
  schema starts a new row. Irrelevant manifest noise outside the declaration
  does not invalidate the key.
- **Builder loop.** `RuntimeManager.work_once` (started from app lifespan when
  `project_runtime` is on) performs at most one Docker build at a time per
  process, with `working_until` lease for crash recovery. All Docker I/O is
  outside `Database.write()`. Failed builds set `state=failed` and
  `reason`; admins may `POST .../runtime/rebuild` to retry the current recipe.
- **Prepare.** `POST /runtime/prepare` with `task_id`, `attempt`, `host_id`,
  `run_as`, `base_ref`, `base_id`, `platform` upserts `runtime_hosts` for the
  agent, ensures a build exists, and either selects a `ready` image (writes
  `task_runtimes`) or **holds** the task (`runtime_preparing`) without
  incrementing attempt. Returns `Selection` (see 3.7). `project_runtime=off`
  yields `state=disabled` and does not hold.
- **Claim gate (3.5).** After an agent has a `runtime_hosts` row, `claim` skips
  tasks whose latest matching build is not `ready` unless none exists yet (first
  prepare creates it). `failed` → task blocked with `runtime_failed` and the
  build reason.
- **Used image.** After the session starts, `POST /runtime/used` compares the
  container's image id to the selected id; mismatch → `409 runtime_image_mismatch`
  and hold. Labels and config are not trusted.
- **Hold.** `POST /runtime/hold` when the worker cannot apply the image or
  environment; sets `runtime_failed` without spending an attempt.
- **Test services.** `argus-test-services` (stdlib Python in the image) reads a
  JSON declaration (`task_id`, services) under `ARGUS_TEST_SERVICES_ROOT`,
  starts unprivileged PostgreSQL and Valkey with synthetic credentials, prints
  `export` lines, and tears down only that task's sockets/data. Task A's `down`
  must not destroy task B's data.
- **Security.** Project images add only catalogue layers and
  `ARGUS_CODEX_TURN_TIMEOUT_MS` when configured; no checkout, PAT, or `.env` in
  the build context. Probe and smoke containers use `--user`, `--cap-drop=ALL`,
  `no-new-privileges`, read-only root with `/tmp` tmpfs where tests require it.
- **NanoClaw integration.** Optional `RuntimeHooks` on `ArgusConfig`: `host`,
  `apply`, `usedImage`, `cleanup`. `ensureRuntime` runs before account/model
  work; `apply` switches the agent-group image only at task start after
  confirming no active session on another image; `cleanup` is per task. Host
  image and agent image remain separate layers.

### 3.7 Integration contract (task continuation and other workers)

Consumers (including the **task-continuation** front) should treat
`argus_core.runtime.Selection` / REST `Selection` as the single projection:

| Field | Meaning |
| --- | --- |
| `state` | `ready` only when the image is selected and every required probe passed; otherwise preparation is incomplete or failed. |
| `declared` | Whether the project has a `project_runtimes` row. |
| `requirement` | Resolved components with `provisioned` vs base-image expectations. |
| `image` | `ref`, `id`, `base_ref`, `base_id`, `platform` actually handed to the worker. |
| `tools` | Named probe evidence (`psql`, `valkey-server`, …). |
| `services` | Kind, env template map, and `argus-test-services run --` command hint. |
| `gaps` | Components not provisioned (optional or catalogue `gap` kind). |
| `task_held` | True when Argus blocked the task pending runtime (no attempt spent). |

`GET /projects/{slug}/runtime` adds admin/history via `RuntimeView` (`builds[]`,
`problems`). `GET /runtime/tasks/{task_id}` lists selected vs `used_image_id` per
attempt. Continuation logic should **not** duplicate these fields; it should
read readiness/gaps from runtime and only ask the worker to start services that
are declared but not yet running locally.

# 4. Open Questions

- Should the dashboard expose runtime declaration editing beyond MCP/API?
- When two branches both add migration `0040`, production needs one merged
  migration number (`0041+`) without editing shipped SQL; see integration notes
  in the delivery report.
- Registry pull of the base/tools image when absent on a remote daemon (today:
  worker must report an id that exists locally).
- Provision paths for `pnpm`, `sops-age`, MinIO, and Elasticsearch when product
  requires them beyond Radar's current gates.

# 5. Constraints

- [Single-node](single-node.spec.md): SQLite tables in migration
  `0041_project_runtime.sql` (integrated with task continuation at `0040`); optional
  feature (`ARGUS_PROJECT_RUNTIME=off`).
- All writes through `Database.write()`; timestamps from `tx.now`; no network
  or Docker inside a write transaction.
- Workers still check out source via repository leases; images never embed repos.
- MCP forwards to REST with the caller's token; no extra business logic in MCP.
- Preserves global account priority, `prompt_trust`, model policy, and OneCLI
  paths; no executor allowlists added by this spec.

# 6. Implementation Consequences

- `argus_core.runtime`: catalogue, `RuntimeSpec`, rendering, probes, `Selection`,
  `declaration`, `contract_services`.
- `argus_core.runtime_assets.argus-test-services`: task-side service lifecycle.
- `argus_api.runtime` (`service`, `docker`, `gate`, `routes`), `RuntimeManager` on
  app state, builder task in `main.py` lifespan.
- `argus_api.tasks.service`: claim gate, `runtime_preparing` / `runtime_failed`
  holds, wake on build ready.
- `argus_api.config`: `project_runtime`, `runtime_image_repo`,
  `runtime_build_timeout_s`.
- `integrations/nanoclaw/argus.ts`: `RuntimeHooks`, `ensureRuntime`,
  `verifyRuntimeUsed`, `holdRuntime`.
- MCP: `get_project_runtime`, `set_project_runtime`, `rebuild_project_runtime`,
  `get_runtime_catalog`, `prepare_project_runtime`, `hold_project_runtime`,
  `verify_project_runtime_used`.
- OpenAPI and `@argus/api-client` regenerated when routes change.
- Tests: `packages/core/tests/test_runtime.py`,
  `test_test_services.py`; `apps/api/tests/test_project_runtime.py`,
  `test_project_runtime_api.py`, `test_mcp.py`; opt-in real Docker
  `test_runtime_docker.py` (`ARGUS_DOCKER_TESTS=1`,
  `ARGUS_RUNTIME_TEST_BASE=<local tools-layer agent image>`).
