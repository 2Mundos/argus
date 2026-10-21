# Local Strata harness (NanoClaw failover)

# 1. Purpose and Scope

When every cloud model account is exhausted or disabled, a NanoClaw worker keeps
working by running Claude Code against the node's **local** Anthropic-compatible
API (Strata on loopback), with **Cursor** (`agent`) as the preferred delegated
executor. Scope: the `local` account provider, lease ranking, engine catalogue,
NanoClaw env (`ANTHROPIC_BASE_URL` allowlist), and the existing `local` engine
for router delegations.

# 2. Non-Goals

- Exposing Strata on the LAN or WAN; the model stays on loopback on the host.
- Routing cloud traffic through the local model when a cloud account is still
  leasable.
- Changing Codex as a harness or making Strata a Codex Responses backend.
- Automatic install of Strata or creation of production accounts (operator).

# 3. Decisions Made

- **`local` account provider.** Credential document: `base_url` (validated:
  `http`/`https`, host ∈ `127.0.0.1`, `localhost`, `::1`, `host.docker.internal`,
  no path/query/credentials), optional `auth_token` placeholder (default
  `argus-local-harness`), optional `model`. No real secret; `max_leases` defaults
  to 1 (single GPU slot). Never marked exhausted for quota; availability follows
  the same probe as `ARGUS_LOCAL_MODEL_URL` via the reaper (`sync_local_health`).
- **Lease ranking.** `fallback_tier`: every non-`local` account sorts before
  `local` (inverted for `trivial` tasks, see [task complexity](task-complexity.spec.md)), then `priority`, load, and deterministic ties (account selection spec).
- **Harness.** NanoClaw `provider` stays `claude`; engine mapping for `local` is
  `claude` on the redirected API. Executor preference: `agent` (Cursor).
- **Container → host.** Strata listens on `127.0.0.1:8080` on the host only (not
  on the LAN). Worker credentials use `http://host.docker.internal:8080` (pool
  `extra_hosts: host.docker.internal:host-gateway`). That name resolves inside the
  container to the Docker bridge gateway IP, which cannot reach a service bound only
  on loopback. While a `local` engine lease is active, the NanoClaw host process
  starts a small TCP listener on the `docker0` address (bridge gateway only),
  forwarding to `127.0.0.1` on the same port (`argus-local-forward.ts`). If
  `docker0` is absent (no Docker bridge), the forwarder is skipped and the
  operator must publish Strata on the bridge address themselves (e.g. `socat`
  `TCP-LISTEN:8080,bind=<bridge-ip>,fork` → `127.0.0.1:8080`). No OneCLI proxy
  and no cloud secret for `api.anthropic.com` on this path; `NO_PROXY` includes
  the local API host so Anthropic traffic does not traverse the egress proxy.
- **Redirect guard.** `api_redirected` is relaxed only when `ANTHROPIC_BASE_URL`
  equals `ARGUS_ALLOWED_ANTHROPIC_BASE_URL` (both set by the host for that
  lease) and the allowed URL's host is one of the local hosts (`127.0.0.1`,
  `localhost`, `::1`, `host.docker.internal`). Any other redirect is rejected;
  redirect plus any cloud credential (`ANTHROPIC_API_KEY`,
  `CLAUDE_CODE_OAUTH_TOKEN`, or non-placeholder `ANTHROPIC_AUTH_TOKEN`) is
  rejected. Local harness mode drops the Claude gateway credential stub and cloud
  sentinel env from the container config.
- **Observability.** `POST /engines/selected` records `provider: local`; task
  views already show `serving_account` and milestones from existing fields.
- **Per-task provider restriction.** A task may carry `account_providers`: a
  non-empty list of engine-capable providers (e.g. `["local"]`), or null
  (unrestricted, the default and the only behaviour before this rule). It is
  validated on create and update against the known engine providers (never
  `gitea`, never a provider unknown to the schema) and is editable only while the
  task is `ready` and unclaimed, under the same rules as other task fields
  ([task edit](task-edit.spec.md)). It is stored in one nullable column on
  `tasks` (migration `0030`).
  - *Lease.* `POST /accounts/lease` with `task_id` intersects the engine candidates
    with the task's list. With `["local"]` only the `local` account is eligible,
    even when cloud accounts are usable. Without a list the ranking is unchanged.
    The restriction only narrows: the live claim, `task_id` and `attempt` rules of
    account selection 3 still apply, and it never lets a worker take an account
    for another task. The executor role is not restricted
    (the `local` account is engine-only anyway).
  - *Claim.* `POST /tasks/claim` skips a restricted task when the caller's
    `runtime_providers` do not intersect its list, or when no account of its
    listed providers is usable right now (available, not exhausted before reset,
    below `max_leases`, probe healthy for `local`). A skipped task stays `ready`
    and spends no attempt, so a busy or offline `local` account holds only the
    restricted task, not the whole queue. Unrestricted tasks are claimed as before.
  - *Surfaces.* The dashboard task form, MCP `create_task` and the API client show
    and accept the field; OpenAPI and client types are regenerated.

# 4. Open Questions

- Task-level “requires cloud” policy for hard tasks (no label in schema yet).
- `trivial` tasks invert the ranking for themselves ([task complexity](task-complexity.spec.md));
  `standard` tasks keep it.
- Whether to add harness model names for `local` in default model policy.

# 5. Constraints

- Single-node spec; no new required service.
- No secrets in container env beyond the public placeholder token Strata ignores.
- Spec-first: behaviour changes stay in this file and `engines.spec.md`.

# 6. Implementation Consequences

- `packages/core`: `LOCAL_PROVIDER`, `validate_local_base_url`, `fallback_tier`,
  `role_accepts` engine-only for `local`.
- `apps/api`: `accounts/service.py` ranking and `sync_local_health`; reaper probe;
  `engines/catalog.py` local entry axes.
- `integrations/nanoclaw`: `argus-accounts.ts` (`local` mapping, `harnessEnvOnly`,
  `preferredExecutorForLease`), `argus-local-harness.ts`, `argus-local-forward.ts`,
  `anthropic-redirect.mjs`, `argus-delegate.mjs` guard, `argus.ts` harness file
  sync (under NanoClaw `DATA_DIR`), `install.sh` OneCLI wrap.
