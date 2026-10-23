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
  `argus-local-harness`), optional `model`, optional `context_tokens` (see "Harness orchestrates only"). No real secret; `max_leases` defaults
  to 1 (single GPU slot). Never marked exhausted for quota; availability follows
  the same probe as `ARGUS_LOCAL_MODEL_URL` via the reaper (`sync_local_health`).
- **Lease ranking.** `fallback_tier`: every non-`local` account sorts before
  `local` (inverted for `trivial` tasks, see [task difficulty](task-difficulty.spec.md)), then `priority`, load, and deterministic ties (account selection spec).
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
  sentinel env from the container config. (A custom harness keeps the stub for its
  `claude` executor, see [custom harness](custom-harness.spec.md).)
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

- **Harness orchestrates only; small context (2026-10-21).** Strata serves only
  the NanoClaw harness. Two runs on the 64k window (db39c512 attempt 77/80,
  ec7bbb45 attempt 54) died with "prompt leaves no room to answer in the context
  (65536)": the harness itself (Claude Code on Strata) had read specs, drafts,
  `AGENTS.md`, `git show` diffs and test files into its own context (40+ shell
  calls, one read-only Cursor delegate); no Claude Code executor ran on Strata. So:
  - *Prompt.* On a local engine lease the task message adds the local harness
    rules: orchestrate only, no bulk reads into the harness context, delegate all
    investigation, implementation, gates and review to `argus-delegate agent`
    (Cursor; `--read-only` for investigation), read only the delegate's JSON
    report line, cap any shell output. A mid-task switch to the local account
    re-renders the resent task with these rules.
  - *No Claude Code executor on Strata.* The harness env carries
    `ARGUS_LOCAL_HARNESS=1`; `argus-delegate claude` refuses there
    (`auth_missing`, reason `local_harness_engine_only`) because it would inherit
    `ANTHROPIC_BASE_URL` and run on the local model. `agent` and `codex` are
    unaffected.
  - *Cursor first.* The executor lease on a local engine lease asks for
    `runtime_providers: ["cursor"]` first (falling back to any provider on
    `no_account`), so the preferred `agent` has its Cursor stub even when a cloud
    Anthropic account outranks Cursor. Only acquisition prefers Cursor: the prompt names
    the CLI of the executor actually leased (`agent`, `codex`; `claude` is never
    recommended on a local engine, the delegate refuses it), or states that delegation
    is blocked for lack of an executor account. Same rule on a custom engine lease
    ([custom harness](custom-harness.spec.md)), whose `claude` executor runs only on a
    real Anthropic account through the gateway.
  - *Safety net: compaction sized to the window.* The local credential may name
    `context_tokens` (an integer, 4096 to 4 000 000; per account). When absent,
    `POST /accounts/lease` fills it from the local model probe's `n_ctx`; when
    unknown, the harness assumes 32 768. The harness env then sets Claude Code
    2.1.296's knobs (verified in the bundled binary): `CLAUDE_CODE_MAX_CONTEXT_TOKENS`
    (the window of a model Claude Code does not know; otherwise it assumes 200k
    and never compacts), `CLAUDE_CODE_AUTO_COMPACT_WINDOW` = 70 % of it,
    `CLAUDE_CODE_MAX_OUTPUT_TOKENS` = min(8192, window/8), and tool output caps
    `CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS` = 4000, `BASH_MAX_OUTPUT_LENGTH` =
    8000. Cloud mode clears them all.

# 4. Open Questions

- Task-level “requires cloud” policy for hard tasks (no label in schema yet).
- `trivial` tasks invert the ranking for themselves ([task difficulty](task-difficulty.spec.md));
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
