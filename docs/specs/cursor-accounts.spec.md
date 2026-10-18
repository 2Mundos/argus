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
  host (`POST https://api2.cursor.sh/auth/exchange_user_api_key`); the worker
  never performs that exchange. OAuth logins use `accessToken` only;
  `refreshToken` stays in Argus and is never sent to OneCLI or the container.
  The container holds a **sentinel** (`CURSOR_API_KEY` and/or a stub
  `auth.json`); the gateway injects the real bearer token only for
  `api2.cursor.sh` on paths `/agent.v1.*` and `/aiserver.v1.*` (generic
  secrets, one per path family). Other hosts and paths are forwarded untouched.
- **Pool configuration**: `nanoclaw.executor_providers` (env
  `ARGUS_NANOCLAW_EXECUTOR_PROVIDERS` on the pool, `ARGUS_EXECUTOR_PROVIDERS` on
  the channel) lists providers for the executor lease, for example `["cursor"]`
  to direct one worker at Cursor while the default executor remains
  `["anthropic"]` when unset.

# 2. Non-Goals

- Reading host `~/.config/cursor`, live Argus secrets, or `.env` from agents.
- Mounting a full imported auth JSON into the worker (no raw credential mount).
- Refreshing OAuth access tokens inside the channel or worker; expired or
  near-expired access tokens are rejected at apply time (five-minute margin,
  matching the shipped CLI).
- Replacing Claude/Codex as the default engine or global executor.
- Running a paid Cursor model call in CI; parent smoke uses fleet with a real
  leased account.

# 3. Decisions Made

- **Account shape** (`packages/core/src/argus_core/cursor_auth.py`): two shapes,
  never mixed; validation on REST create/replace and in the channel before
  OneCLI apply. Responses never echo token values.
- **Executor-only provider**: `cursor` is listed in `EXECUTOR_ONLY`; pool
  validation rejects it in `account_providers` and accepts it only in
  `executor_providers`.
- **OneCLI mapping** (`integrations/nanoclaw/argus-accounts.ts`): two generic
  secrets per agent group (`argus-<group>-cursor-agent` and
  `argus-<group>-cursor-aiserver`), same access token value, distinct path
  patterns. Host pattern is exactly `api2.cursor.sh`.
- **Child runtime** (`argus-cursor-stub.ts`, `argus-delegate`): generic Cursor
  secrets do not add `CURSOR_*` to `container-config` env (only proxy + CA).
  The trusted host detects assignment via `GET /api/agents/{id}/secrets` and
  secret metadata (`type: generic`, `hostPattern: api2.cursor.sh`, path
  patterns), then mounts sentinel `~/.config/cursor/auth.json` (fake
  `accessToken` + placeholder `refreshToken`) and `cli-config.json` with
  `network.useHttp1ForAgent: true`. Never set `CURSOR_API_KEY` in the worker
  (that would call `/auth/exchange_user_api_key` locally). `argus-delegate`
  treats only `auth.json` presence as authenticated.
- **Engine catalogue**: Cursor remains `role: executor` only; readiness still
  requires a container credential (`CURSOR_API_KEY` or stub `auth.json`), not an
  Argus lease by itself.

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
- Default executor providers remain `anthropic` when unset.

# 6. Implementation Consequences

- `packages/core`: `cursor_auth.py`; fleet `executor_providers` validation.
- `apps/api`: cursor validation on create/replace; pool env
  `ARGUS_EXECUTOR_PROVIDERS`; tests in `test_cursor_accounts.py`.
- `integrations/nanoclaw`: `argus-accounts.ts` (mapping, exchange, apply),
  `argus-cursor-stub.ts`, `argus.ts` (`ARGUS_EXECUTOR_PROVIDERS`),
  `argus-delegate` (`prepareExecutorEnv`: copy sentinel stubs into a per-run
  writable `CURSOR_CONFIG_DIR` when gateway mounts are read-only; native CLI
  atomic config writes never target RO stub paths), `install.sh` hooks into
  `onecli.ts`.
- Acceptance: `make check`; channel tests after `install.sh` into a NanoClaw
  checkout; parent bounded fleet smoke with `executor_providers: ["cursor"]` on
  one pool.
