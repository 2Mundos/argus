# Local Strata harness (NanoClaw failover)

# 1. Purpose and Scope

When every cloud model account is exhausted or disabled, **NanoClaw** (the
harness) keeps working against the node's **local** model (Strata on loopback):
its Anthropic-compatible provider talks to Strata, and **Cursor** (`agent`) is
the preferred delegated executor. The same `local` account may also be leased as
an **executor**; coding work on that model goes through **OpenCode** (OpenAI-
compatible), never through `argus-delegate claude`. Scope: the `local` account
provider, its endpoints per runtime, its health, the admin test and editor, lease
ranking, engine catalogue, NanoClaw env (`ANTHROPIC_BASE_URL` allowlist), OpenCode
lease settings, and the existing `local` engine for router delegations.

One account is reached from two places that do not share a network: worker
containers, and the Argus API process itself (the
[built-in orchestrator](builtin-orchestrator.spec.md), the health probe and the
admin test). On a node whose API runs on the host, `host.docker.internal` resolves
only inside containers, so a single URL cannot serve both. This spec gives the
account one endpoint per runtime.

# 2. Non-Goals

- Exposing Strata on the LAN or WAN; the model stays on loopback on the host.
- Routing cloud traffic through the local model when a cloud account is still
  leasable.
- Changing Codex as a harness or making Strata a Codex Responses backend.
- Automatic install of Strata or creation of production accounts (operator).
- Testing the worker endpoint from the API process: it lives in another network
  namespace. Workers report their own failures through their task.
- Automatic inference. Only an admin's explicit test sends a completion; the
  health probe only lists models.
- Hosts-file entries, a raw bridge IP in a credential, or a new required service.

# 3. Decisions Made

- **`local` account provider.** Credential document: `base_url` (the worker
  endpoint), optional `api_base_url` (the API endpoint, below), optional
  `auth_token` placeholder (default `argus-local-harness`), optional `model`,
  optional `context_tokens` (see "Harness orchestrates only"). Both URLs are
  validated alike: `http`/`https`, host ∈ `127.0.0.1`, `localhost`, `::1`,
  `host.docker.internal`, no path/query/credentials. A raw bridge IP is refused.
  No real secret; `max_leases` defaults to 1 (single GPU slot). Never marked
  exhausted for quota; availability follows its own health probe (below). The
  same validation applies on create, on the local editor, and on
  `PUT /api/accounts/{id}/credential`, so no path stores an unchecked URL. The
  account form can detect a running Ollama, LM Studio or llama.cpp server and fill
  these fields ([built-in orchestrator](builtin-orchestrator.spec.md) 3.11). The
  built-in orchestrator may also lease a `local` account as its engine.
- **One endpoint per runtime.** The *worker* runtime is any agent in a container
  (NanoClaw workers, a NanoClaw orchestrator, OpenCode). The *API* runtime is the
  Argus process: the built-in orchestrator (an agent of a pool whose `runtime` is
  `builtin`), the health probe and the admin test. The worker endpoint is
  `base_url`. The API endpoint, first match wins:
  1. `api_base_url` when set (source `api_base_url`);
  2. when Argus does not run in a container (`/.dockerenv` absent) and
     `base_url`'s host is `host.docker.internal`, the same scheme and port on
     `127.0.0.1` (source `derived`): the host side of the bridge forwarder below;
  3. `base_url` (source `base_url`), e.g. an API container that reaches
     `host.docker.internal` through `deploy/compose.docker.yaml`.

  Existing accounts carry no `api_base_url`, so they keep their worker endpoint
  and gain a working API endpoint without an edit. A lease names the endpoint of
  the runtime that holds it: `settings.base_url` and `credential.base_url` are the
  API endpoint for a built-in agent and `base_url` for every other agent. Every
  lease credential carries only `base_url`, `auth_token`, `model` and
  `context_tokens`, the keys a NanoClaw already deployed accepts, so workers need
  no update.
- **Health per account.** The reaper probes each local account from the API
  runtime every `ARGUS_LOCAL_HEALTH_INTERVAL_S` (default 30 s, the first probe one
  interval after start): `GET {API endpoint}/v1/models` with the account's
  placeholder token, a 5 s timeout and no redirects, outside any write
  transaction. Accounts sharing an endpoint share one request. The models the
  account runs are its `model` (workers; any served model when it names none) and,
  when `orchestrator.model` is `local/<model>`, that model (the built-in
  orchestrator). The outcome:
  - the endpoint fails (DNS, refused, timeout, TLS, redirect, a non-200 status or
    an unreadable list): Argus disables the account with
    `local model unreachable: <failure> at <endpoint>`;
  - it answers, but none of the models the account runs is served: Argus disables
    it with `local model unavailable: <model> is not served at <endpoint>`;
  - otherwise it is healthy, and an account Argus disabled for either reason is
    available again. A model the account runs that is not served while another is
    (a stale account `model` behind a served `orchestrator.model`, or the
    reverse) is a *stale model* warning, never a disable.

  `ARGUS_LOCAL_MODEL_URL` no longer decides any account's health; it stays the
  `local` engine's endpoint ([engines](engines.spec.md)). A lease whose account
  names no `context_tokens` or no `model` fills them from that account's own probe
  (`meta.n_ctx`, or llama.cpp `/props`; the first served model), never from
  another endpoint. The probe sends no inference and records nothing but the
  account state; its last answer is kept in memory for the account view.
- **Admin test.** `POST /api/accounts/{id}/local/test`, admin only, body
  `{inference: false, tool_calling: false}`. It runs from the API runtime against
  the account's own API endpoint, never another probe, and answers three separate
  statuses (`passed`, `failed`, `skipped` or `not_run`), each with its model,
  latency and failure class:
  - *discovery* (always): DNS and connection, then `/v1/models`, then whether the
    account's `model` and the built-in orchestrator's model are listed;
  - *inference* (only with `inference: true`): one chat completion of at most 256
    output tokens on the account's model; `passed` only when the reply holds
    non-empty `content` (a reply with only `reasoning_content`, or none, is
    `empty_answer`); `skipped` when discovery failed or the model is not served;
  - *orchestrator* (only with `tool_calling: true`): one chat completion of at
    most 256 output tokens offering one tool with tool choice `auto`, as the
    built-in sends it, on the model the built-in orchestrator would run
    (`orchestrator.model`'s `local/<model>`, else the account's, else the first
    served); `passed` only when the reply holds a call of that tool
    (`no_tool_call` otherwise, `empty_answer` when the budget ran out first).

  256 tokens bounds the cost and still leaves a reasoning model room to answer.

  A discovery pass never marks inference or the orchestrator check passed. The
  answer also names the worker endpoint and says the API cannot test it from its
  own network. The result is kept in memory as the account's last test (shown
  until the endpoints or model change) and recorded as `account.local_tested`
  with the statuses and failure classes only.
- **Editor.** `PUT /api/accounts/{id}/local`, admin only, body any of
  `base_url`, `api_base_url`, `model`, `context_tokens`. A field left out is kept;
  `null` clears an optional one (`api_base_url` back to the derived default,
  `model` back to the served model, `context_tokens` back to the probe). The new
  document is validated as on create, written to the secret store, then
  `account.local_updated` names the fields (never values). The answer is the
  account with a fresh discovery, so a model the server no longer lists shows as
  a stale model warning at once. **Reset** clears `api_base_url`, `model` and
  `context_tokens` in one call.
- **Account view.** `GET /api/accounts` gives a local account `local`: both
  endpoints, the API endpoint's source, the stored `model` and `context_tokens`,
  the built-in orchestrator's model and its source, the last discovery, the served
  models, the last test's inference and orchestrator statuses (`not_run` until an
  admin runs one), and the warnings. It never holds `auth_token`.
- **Failures.** One classification for the probe, the test and the built-in
  orchestrator's model calls: `dns_failed`, `connection_refused`, `timeout`,
  `tls_failed`, `connect_failed`, `redirect_refused`, `http_error` (with the
  status), `invalid_response`, `model_not_served`, `no_models`, and for the
  test's completions `empty_answer` and `no_tool_call`. It reads only the
  exception type and errno of Argus's own transport, the HTTP status and the error
  `type` or `code` token; never a response body, header, key or prompt. Replies
  and logs name the account, the endpoint origin, the runtime and, in a turn, the
  turn id, so an admin can match a reply to the account and the log line.
- **Lease ranking.** `fallback_tier`: every non-`local` account sorts before
  `local` (inverted for `trivial` tasks, see [task difficulty](task-difficulty.spec.md)), then `priority`, load, and deterministic ties (account selection spec).
- **Harness is NanoClaw.** The worker loop is NanoClaw. For a `local` engine
  lease its provider stays `claude` only as the Anthropic Messages client pointed
  at Strata (`ANTHROPIC_BASE_URL`); that is not “Claude Code is the harness”.
  Default executor preference: `agent` (Cursor).
- **Custom / local models as executor → OpenCode.** An OpenAI-compatible model
  (`local` or `custom`) used for delegated coding is reached with
  `argus-delegate opencode` and lease `settings`, not with `argus-delegate
  claude`. A `local` account may be leased for the `executor` role. The lease
  carries `settings` `{base_url, label: "local", model}` from the credential (no
  secret). NanoClaw passes those as `executor_settings` on the pinned
  model-policy context; OpenCode applies them as `OPENCODE_CONFIG_CONTENT` (same
  path as [custom accounts](custom-accounts.spec.md)). The public placeholder
  `auth_token` is exposed only as `ARGUS_ACCOUNT_API_KEY` for OpenCode's
  `{env:…}` reference. A task may pin `executor_model: {executor: opencode,
  model}` ([task models](task-models.spec.md)); that pin leases the `local`
  provider for the executor role. Default acquisition still prefers Cursor.
- **Container → host.** Strata listens on `127.0.0.1:8080` on the host only (not
  on the LAN). Worker credentials use `http://host.docker.internal:8080` (pool
  `extra_hosts: host.docker.internal:host-gateway`). That name resolves only inside
  the container, to the Docker bridge gateway IP, which cannot reach a service
  bound only on loopback. While a `local` engine **or** executor lease is active,
  the NanoClaw host process starts a small TCP listener on the `docker0` address
  (bridge gateway only), forwarding to `127.0.0.1` on the same port
  (`argus-local-forward.ts`). If `docker0` is absent (no Docker bridge), the
  forwarder is skipped and the operator must publish Strata on the bridge
  address themselves (e.g. `socat` `TCP-LISTEN:8080,bind=<bridge-ip>,fork` →
  `127.0.0.1:8080`). No OneCLI proxy and no cloud secret for `api.anthropic.com`
  on this path; `NO_PROXY` includes the local API host so Anthropic traffic does
  not traverse the egress proxy. An API on the host reaches the same server at
  `127.0.0.1` on that port (the derived API endpoint above), so neither runtime
  needs a hosts entry.
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
    for another task. `account_providers` still applies only to the engine role;
    an `executor_model` of `opencode` narrows the executor lease to `local`
    ([task models](task-models.spec.md) 3.2).
  - *Claim.* `POST /tasks/claim` skips a restricted task when the caller's
    `runtime_providers` do not intersect its list, or when no account of its
    listed providers is usable right now (available, not exhausted before reset,
    below `max_leases`, probe healthy for `local`). A skipped task stays `ready`
    and spends no attempt, so a busy or offline `local` account holds only the
    restricted task, not the whole queue. Unrestricted tasks are claimed as before.
  - *Surfaces.* The dashboard task form, MCP `create_task` and the API client show
    and accept the field; OpenAPI and client types are regenerated.

- **NanoClaw orchestrates; OpenCode (or Cursor) executes.** Two runs on the 64k
  window (db39c512 attempt 77/80, ec7bbb45 attempt 54) died with "prompt leaves
  no room to answer in the context (65536)": NanoClaw's agent loop on Strata had
  read specs, drafts, `AGENTS.md`, `git show` diffs and test files into its own
  context (40+ shell calls, one read-only Cursor delegate). So:
  - *Prompt.* On a local engine lease the task message adds the local harness
    rules: orchestrate only, no bulk reads into the harness context, delegate all
    investigation, implementation, gates and review to the leased executor
    (default `argus-delegate agent` / Cursor; `--read-only` for investigation;
    or `opencode` when that executor is leased or pinned), read only the
    delegate's JSON report line, cap any shell output. A mid-task switch to the
    local account re-renders the resent task with these rules.
  - *No `argus-delegate claude` on a local/custom model.* The local engine env
    carries `ARGUS_LOCAL_HARNESS=1`; `argus-delegate claude` refuses there
    (`auth_missing`, reason `local_harness_engine_only`) because it would inherit
    `ANTHROPIC_BASE_URL` and treat Claude Code as the client of the custom/local
    model. For those models the coding executor is OpenCode (or Cursor on a
    separate account). `agent`, `codex` and `opencode` are unaffected.
  - *Cursor first (unless the difficulty chain is empty).* The executor lease on
    a local engine lease asks for `runtime_providers: ["cursor"]` first (falling
    back to any provider on `no_account`), so the preferred `agent` has its Cursor
    stub even when a cloud Anthropic account outranks Cursor. An empty/`none`
    difficulty executor chain ([task difficulty](task-difficulty.spec.md)) skips
    the executor lease: NanoClaw does the work on the local model and the prompt
    does not require `argus-delegate`. An `executor_model` pin of `opencode`
    leases `local` for the executor role. Otherwise the prompt names the CLI
    leased (`agent`, `codex`, `opencode`; `claude` is never recommended on a local
    engine), or states that delegation is blocked. Same Cursor-first default on a
    custom engine lease ([custom harness](custom-harness.spec.md)).
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

- **Entry points.** One service (`accounts/local_health.py`) serves every
  transport, behind the same admin check.

  | Capability | Dashboard | REST | MCP |
  | --- | --- | --- | --- |
  | See endpoints, health, warnings | Accounts row and the Local endpoints dialog | `GET /api/accounts` (`local`) | `list_accounts` |
  | Edit endpoints or model, reset | Local endpoints dialog: Save, Reset | `PUT /api/accounts/{id}/local` | `update_local_account` (admin) |
  | Test discovery, inference, tool calls | Local endpoints dialog: three Test buttons | `POST /api/accounts/{id}/local/test` | `test_local_account` (admin) |

- **Engines.** Claude, Codex, Cursor, Copilot and custom accounts are unchanged:
  their URLs, leases and errors do not read the local endpoints. Local: this
  spec. A custom harness keeps its own rules ([custom harness](custom-harness.spec.md)).
- **Way back.**

  | Way in | Way out | Where it shows |
  | --- | --- | --- |
  | `api_base_url` set | Clear it or Reset: the derived or worker endpoint again | Local endpoints dialog, `local.api_endpoint_source` |
  | `model` changed | Set another, or clear it to run the served model | the same, stale model warning |
  | Disabled by its health probe | Enabled by the next healthy probe, or Enable by hand | Accounts state and reason |
  | A test's statuses | Change the endpoints or model (clears them), or test again | Local endpoints dialog |

# 4. Open Questions

- Task-level “requires cloud” policy for hard tasks (no label in schema yet).
- `trivial` tasks invert the ranking for themselves ([task difficulty](task-difficulty.spec.md));
  `standard` tasks keep it.
- Whether to add harness model names for `local` in default model policy.

# 5. Constraints

- Single-node spec; no new required service and no migration: endpoints live in
  the credential document, health in the account state, the last probe and test
  in memory.
- No secrets in container env beyond the public placeholder token Strata ignores.
- No network call inside a write transaction: probes and tests run before the
  write that records them.
- Spec-first: behaviour changes stay in this file and `engines.spec.md`.

# 6. Implementation Consequences

- `packages/core`: `LOCAL_PROVIDER`, `validate_local_base_url`, `fallback_tier`,
  `role_accepts` for engine and executor, `local_lease_settings`,
  `local_api_endpoint`, `LOCAL_WORKER_KEYS`, the local view, test and update
  models, `ExecutorModel` / `EXECUTOR_PROVIDERS` for `opencode` → `local`.
- `apps/api`: `accounts/service.py` ranking, `_settings` for local,
  `sync_local_health` per account; `accounts/local_health.py` (endpoints,
  discovery, failure classes, test, editor, lease delivery); the reaper's probe
  in `main.py`; the local routes; MCP `update_local_account` and
  `test_local_account`; `orchestrator_builtin/models` failure classes and turn
  replies; `engines/catalog.py` local entry axes; harness-account pin allows
  `executor_model` `agent` or `opencode` with local.
- Dashboard: `LocalEndpoints.tsx` (edit, test, reset) and the Accounts row;
  regenerated `openapi.json` and `schema.d.ts`.
- Tests: per-runtime endpoints (host API with Docker workers, API in a
  container), lease credentials without API-only keys, per-account health (DNS
  failure, refused, changed or missing models, the `orchestrator.model`
  override), the test's separate statuses and SSRF and redirect refusals, the
  editor's validation and reset, sanitized turn replies, and the dashboard dialog.
- `integrations/nanoclaw`: `argus-accounts.ts` (`local` mapping, `harnessEnvOnly`,
  `preferredExecutorForLease`, `executorFirst`), `argus-local-harness.ts`,
  `argus-local-forward.ts` (engine or executor lease), `anthropic-redirect.mjs`,
  `argus-delegate.mjs` guard and OpenCode settings, `argus.ts` harness/settings
  sync (under NanoClaw `DATA_DIR`), `install.sh` OneCLI wrap.
