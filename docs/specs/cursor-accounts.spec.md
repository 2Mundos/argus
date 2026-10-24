# Cursor accounts: import, executor lease, delegated CLI

Builds on [engines](engines.spec.md) and the account pool in
[worker lifecycle 3.9](worker-lifecycle.spec.md). Single-node rules apply
([spec](single-node.spec.md)): no new service; reuse the existing `executor`
lease role.

# 1. Purpose and Scope

Make a Cursor login or API key a normal Argus **account**, validate its shape
on create and replace, and deliver it to the **delegated** Cursor CLI
(`argus-delegate agent`) without making Cursor an engine harness:

- **Import**: the operator stores either `{"api_key": …}` or an imported
  `agent login` document (`accessToken` + `refreshToken`, both JWT-shaped for
  the access token). A Cursor CLI settings file (`cli-config.json` with
  permissions, editor, model, `authInfo`) is rejected; errors name fields only.
- **Lease**: the `executor` lease role selects a `cursor` account next to the
  engine's account (for example Codex or Claude). Cursor is never an `engine`
  lease and never replaces the primary harness.
- **Delivery**: the NanoClaw channel (trusted host) writes the credential into
  the OneCLI vault. API keys are exchanged for a short-lived access token on the
  host only (`POST https://api2.cursor.sh/auth/exchange_user_api_key`); the
  worker never performs that exchange. OAuth logins use the exchanged or imported
  `accessToken` on the host; `refreshToken` stays in Argus, is never sent to
  OneCLI or the container, and is **not** refreshed automatically by the channel
  (operators renew in Argus when access nears expiry).
  The worker holds a **non-secret sentinel** only: a stub `auth.json` with a
  fake JWT-shaped `accessToken` and a placeholder `refreshToken` (never a real
  provider token). The gateway injects the real bearer token only for
  `api2.cursor.sh` on paths `/agent.v1.*` and `/aiserver.v1.*` (generic
  secrets, one per path family). **`CURSOR_API_KEY` is never set in the worker**
  (the native CLI would call `/auth/exchange_user_api_key` locally). Other hosts
  and paths are forwarded untouched.
- **Executor selection**: the executor lease is the best usable account across
  all runtime-supported providers by priority
  ([global account selection](account-selection.spec.md)); a Cursor account is
  leased as the executor when it ranks first. The former pool/env
  `executor_providers` list is retired and ignored.

# 2. Non-Goals

- Reading host `~/.config/cursor`, live Argus secrets, or `.env` from agents.
- Mounting a full imported auth JSON into the worker (no raw credential mount).
- Refreshing OAuth access tokens inside the channel or worker; expired or
  near-expired access tokens are rejected at apply time (five-minute margin,
  matching the shipped CLI).
- Replacing Claude/Codex as the default engine or global executor.
- Running a paid Cursor model call in CI; fleet acceptance uses a parent-run
  bounded smoke with a real leased account (see [fleet smoke ref](../runbooks/2026-10-18.cursor-fleet-smoke.ref.md)).

# 3. Decisions Made

- **Account shape** (`packages/core/src/argus_core/cursor_auth.py`): two shapes,
  never mixed; validation on REST create/replace and in the channel before
  OneCLI apply. Responses never echo token values.
- **Quota** ([account usage](account-usage.spec.md) 3.3): an imported login
  (`kind=subscription`) is read by Argus from `api2.cursor.sh`
  `aiserver.v1.DashboardService` (`GetCreditGrantsBalance`,
  `GetUsageLimitStatusAndActiveGrants`, `GetPlanInfo`; Connect JSON, int64 as
  strings) with the stored `accessToken` only, read-only, never refreshed, never
  logged. A numeric credit grant becomes credit in USD; a login that states none
  reads `no_usage_window_for_login`, not 0. An **API key** (`kind=api_key`) is a
  different case: its usage is the team Admin API's (an admin key), so it reads
  `admin_key_required`, and the login path does not apply to it.
- **Executor-only provider**: `cursor` is listed in `EXECUTOR_ONLY`; the `engine`
  lease role never returns it, the `executor` role may.
- **OneCLI mapping** (`integrations/nanoclaw/argus-accounts.ts`): two generic
  secrets per agent group (`argus-<group>-cursor-agent` and
  `argus-<group>-cursor-aiserver`), same access token value, distinct path
  patterns. Host pattern is exactly `api2.cursor.sh`.
- **Child runtime** (`argus-cursor-stub.ts`, `argus-delegate`): generic Cursor
  secrets do not add `CURSOR_*` to `container-config` env (only proxy + CA).
  The trusted host detects assignment via OneCLI agent/secret **metadata** (no
  secret values), then mounts read-only sentinel stubs at
  `/home/node/.config/cursor/auth.json` and `cli-config.json` (HTTP/1.1 preset).
  `argus-delegate` treats stub `auth.json` presence under `XDG_CONFIG_HOME` as
  authenticated. Because the native CLI atomically rewrites `cli-config.json`
  via temp files in `CURSOR_CONFIG_DIR`, `prepareExecutorEnv` copies sentinel
  `auth.json` and HTTP/1.1 `cli-config.json` into a **per-run writable tree**
  under `{authorized cwd}/.argus-delegate-runtime/cursor-config/` when gateway
  mounts are read-only; RO stub paths are never mutated. Read-only delegation
  uses `--trust` on the already authorized workspace cwd and does **not** pass
  `--force` (tool permissions); write mode may pass `--force` as today.
- **Engine catalogue** (public API): Cursor is `role: executor` only. Readiness
  for a running worker is **stub `auth.json` present** (sentinel session shape)
  plus gateway path-scoped injection configured on the host—not `CURSOR_API_KEY`
  in fleet workers and not an Argus lease visible inside the container alone.
  Catalogue fields such as `usable` / `authenticated` / `validated` reflect that
  delegated path after lease apply, not raw env API keys in the child.

# 4. Open Questions

- Whether OneCLI will add more Cursor API path families; new routes need an
  explicit generic secret each (no broad host wildcard).
- OAuth refresh on the trusted host without giving the refresh token to the CLI
  (out of scope until designed; until then operators renew in Argus).

# 5. Constraints

- Admin scope for import and credential replace. Workers lease only; the container
  never sees the real access token, refresh token, or API key.
- API key exchange and lease apply run on the NanoClaw host, not in the worker
  container.
- No credential value in a response, listing, event, log, or error.

# 6. Implementation Consequences

- `packages/core`: `cursor_auth.py`; `accounts.py` role eligibility.
- `apps/api`: cursor validation on create/replace; tests in
  `test_cursor_accounts.py`.
- `integrations/nanoclaw`: `argus-accounts.ts` (mapping, exchange, apply),
  `argus-cursor-stub.ts`, `argus.ts` (executor lease),
  `argus-delegate` (`prepareExecutorEnv`, `--trust`, private `CURSOR_CONFIG_DIR`),
  `install.sh` hooks into `onecli.ts`.
- **Acceptance (automated)**: `make check`; channel/gateway tests after
  `install.sh` into a NanoClaw checkout; fake-credential delegate/proxy probes
  in the integration scripts (no live Cursor model in CI).
- **Acceptance (fleet)**: parent-run `argus-delegate agent --read-only` with an
  explicit model (for example `composer-2.5`), **native CLI exit code 0**, and an
  exact controlled nonce echoed in the CLI JSON result—not merely Argus task
  completion. Record outcomes in a dated ref when verified; see
  [2026-10-18 cursor fleet smoke](../runbooks/2026-10-18.cursor-fleet-smoke.ref.md).
