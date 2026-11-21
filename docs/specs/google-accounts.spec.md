---
status: implemented
created: 2026-11-19
updated: 2026-11-21
---

# Google (Antigravity) accounts: import, executor lease, delegated CLI

Builds on [engines](engines.spec.md), the account pool in
[worker lifecycle 3.9](worker-lifecycle.spec.md) and the executor pattern of
[Cursor accounts](cursor-accounts.spec.md) and
[Copilot accounts](copilot-accounts.spec.md). Single-node rules apply
([spec](single-node.spec.md)): no new service; reuse the existing `executor`
lease role.

# 1. Purpose and Scope

Make a Google Antigravity credential (either a Gemini API key or an imported
Google OAuth subscription login) a normal Argus **account**, validate its shape
on create and replace, and deliver it to the **delegated** Antigravity CLI
(`argus-delegate agy`) without making Antigravity an engine harness:

- **Import**: the operator stores either `{"api_key": …}` (`kind: "api_key"`)
  or an imported `agy` OAuth login document (`kind: "subscription"`): either the
  native `antigravity-oauth-token` shape
  `{"token": {"access_token", "refresh_token", "expiry", …}, "auth_method": "consumer"}`
  or the flat shape `{"access_token", "refresh_token", "expiry"}`. Settings files
  (`settings.json`, `config.json`) without a credential are rejected; errors name
  fields and rules only, never values.
- **Lease**: the `executor` lease role selects a `google` account next to the
  engine's account (for example Codex or Claude). Google is never an `engine`
  lease and never replaces the primary harness.
- **Delivery**: the NanoClaw channel (trusted host) writes the credential into
  the OneCLI vault using generic path-scoped secrets. The worker holds only a
  **non-secret sentinel**, and the gateway injects the real credential on the
  wire for the hosts and paths `agy` authenticates against (§3, delivery). The
  real key or OAuth token never enters the container, image, checkout or a log.
- **Executor selection**: the executor lease is the best usable account across
  all runtime-supported providers by priority
  ([global account selection](account-selection.spec.md)); a Google account is
  leased as the executor when it ranks first or when an execution chain / task
  pin selects `agy`/`google`.

# 2. Non-Goals

- Reading host `~/.gemini/antigravity-cli`, the OS keyring, `gcloud` ADC, live
  Argus secrets or `.env` from agents. Import is something the operator does,
  naming the credential; Argus never looks in those places.
- Refreshing Google OAuth access tokens inside the NanoClaw channel or worker
  container; refresh runs only in the Argus control plane (which owns the SOPS
  store) before leasing, never in workers. The channel still rejects expired or
  near-expired tokens at apply time (five-minute margin, matching Cursor).
- A dedicated interactive OAuth login wizard driven by Argus (the Codex and
  Claude pattern). v1 is import only.
- Making Antigravity an engine harness; NanoClaw has no Antigravity provider.
- Reading Google/Antigravity quota from an undocumented endpoint: quota stays
  `unknown` with `no_provider_source`, never `0`.
- Running a paid Google/Antigravity model call in CI; fleet acceptance is a
  parent-run bounded smoke with a real leased account.

# 3. Decisions Made

- **Account shape** (`packages/core/src/argus_core/google_auth.py`):
  `provider: "google"`, two mutually exclusive shapes, never mixed:
  - API key (`kind: "api_key"`): `{"api_key": …}`. Rejects empty, whitespace,
    extra keys, or OAuth tokens (`ya29.` / `1//`) pasted as an API key.
  - OAuth subscription (`kind: "subscription"`): either the native
    `antigravity-oauth-token` document (`{"token": {"access_token": …}, …}`) or
    a flat object with `access_token` (and optional `refresh_token`, `expiry`,
    `token_type`, `expires_in`, `auth_method`). Additional CLI metadata fields
    (such as `id_token_email` or `scope`) are tolerated and ignored, never
    echoed. Rejects API keys (`AIza…`) in `access_token`, malformed `expiry`, or
    expired access tokens without a `refresh_token` at import. When
    `refresh_token` is supplied, an expired `access_token` is accepted so the
    control plane can refresh it before leasing; `refresh_token` is stored in
    SOPS and never enters worker containers.
  Kind is derived from the credential on create and credential update. Responses
  and events never echo any token or key.
- **Dashboard onboarding**: the Add account wizard's **Google AI Studio** tile
  opens a dedicated dialog (name and Gemini API key) that posts
  `provider: "google"`, `kind: "api_key"`, `credential: {"api_key": …}`. It never
  opens the custom-provider form. The **Custom provider** form keeps a "Google
  (OpenAI-compatible)" preset (`provider: "custom"`, label `google`) for the
  OpenAI-compatible endpoint only; such an account is never leased to `agy`.
- **Remediation of Google AI Studio keys saved as `custom`** (before this
  change the tile created `provider: "custom"`, label `google`). Nothing is
  migrated automatically: Argus never reads a stored key back or rewrites a
  credential or unrelated account.
  1. Find them (read-only): `GET /api/accounts`, keep `provider == "custom"` with
     `config.label == "google"` or a `config.base_url` on
     `generativelanguage.googleapis.com`.
  2. For each one the operator wants for Antigravity, add a new account with the
     Google AI Studio tile (or `POST /api/accounts` with `provider: "google"`),
     entering the Gemini key again. Check it under Engines (Antigravity
     `accounts.available`).
  3. Keep the old custom account if its OpenAI-compatible use (harness, custom
     executor) is still wanted; otherwise disable and then delete it. Skipping
     this step is safe, since the two accounts are independent.
- **OAuth token refresh** (`apps/api/src/argus_api/accounts/google_refresh.py`):
  Google OAuth access tokens expire after 1 hour (`expires_in: 3600`). Before
  `POST /api/accounts/lease` (for an `executor` lease that includes `google`)
  and before `POST /api/tasks/claim` (pre-reservation and again right before
  execution-chain `executor_delivery`, since claims send engine-only
  `runtime_providers` and may long-poll), the control plane inspects usable
  Google OAuth accounts outside any write transaction and refreshes any
  credential whose `expiry` is within 10 minutes (or already past) and that
  carries a `refresh_token`, serialized per account:
  - **Direct HTTP**: when `client_id` and `client_secret` are provided in the
    credential document or via `ARGUS_GOOGLE_OAUTH_CLIENT_ID` /
    `ARGUS_GOOGLE_OAUTH_CLIENT_SECRET`, posts `grant_type=refresh_token` to
    `https://oauth2.googleapis.com/token`. No upstream OAuth client secret is
    committed in Argus.
  - **CLI-delegated fallback**: otherwise, resolves `agy` in order from
    `ARGUS_AGY_BIN`, `PATH`, `~/.local/bin/agy`, or the pinned `agy 1.5.12`
    client downloaded on first use into `<data_dir>/tools/agy-1.5.12/agy` via
    `accounts/clients.py` (SHA-512 verified, matching `install-agy.sh`;
    disabled when `ARGUS_CLIENT_DOWNLOADS=0`, so containerized API deploys
    refresh automatically without bundling `agy` in the API image). Stages the
    credential with a past `expiry` in an isolated `0700` temp `HOME`
    (scrubbing D-Bus, keyring, `XDG_RUNTIME_DIR` and API-key env vars, with
    `NO_BROWSER=1`), runs
    `agy --output-format stream-json --model __argus_refresh_probe__ -p .` so
    `agy` refreshes the token and exits before any model call, validates the
    updated file, and removes the temp directory.
  - Refreshed credentials preserve original shape (native or flat), extra
    metadata fields, and any rotated `refresh_token`, and are written back to
    SOPS. Permanent rejection (`invalid_grant` / `unauthorized_client`) records
    `account.auth_rejected` with a fixed non-secret reason so the account is not
    repeatedly leased; transient errors leave it eligible for retry.
- **Mid-lease credential refresh**: claim/lease delivery is a snapshot, so a task
  that outlives the delivered token (refreshed only when within 10 minutes of
  expiry) would present an expired token on later invocations, and an
  execution-chain lease cannot be re-leased (`execution_lease_owned`).
  `POST /api/accounts/{account_id}/credential/refresh`
  (`{task_id, attempt, role, force}`) returns the lease with a current credential.
  It requires the live claim and the caller's live lease on the account (released,
  superseded or stale attempt: `409`, e.g. `not_leased`), refreshes through the
  same path as above, and fails closed, never returning the stale credential:
  `409 credential_refresh_rejected` (permanent; records `account.auth_rejected`,
  renew the account) or `503 credential_refresh_unavailable` (transient). Provider
  traffic is bounded to one attempt per account per 60 s (`ATTEMPT_COOLDOWN`): a
  failure or a forced refresh inside the window is answered from the last outcome.
  A failed attempt also stands for the window against a non-forced call on a
  token that still looks fresh. The token whose refresh failed stays invalid
  (remembered in process memory, independent of the cooldown) until a refresh
  or a credential replacement changes it: after the window the provider is
  retried once, never answered with the rejected token. The token is marked
  before the provider is awaited, so a plain call that overlaps an in-flight
  refresh waits for its outcome instead of answering from the token being
  refreshed. A forced call inside the
  window after a successful refresh (a 401 on the new token) marks that token
  rejected without contacting the provider; a plain call then answers
  `503 credential_refresh_unavailable` until the window ends or the token is replaced. An access-only credential (no
  `refresh_token`) past its expiry is `409 credential_refresh_rejected`.
  `force` refreshes a token whose expiry is not near, for a provider 401 on it.
  Tested deterministically with fake tokens and an injected token endpoint
  (`apps/api/tests/test_google_accounts.py`). The historical 401 of task 62c4b502
  is not proven to be this mechanism. Not covered offline: the worker calling
  this endpoint on a 401 and re-applying the credential (separate follow-up), and
  a live Google refresh, which needs separately authorized live verification.
- **Executor-only provider**: `google` joins `cursor` and `copilot` in
  `EXECUTOR_ONLY`; the `engine` lease role never returns it, the `executor` role
  may. The `agy` executor runs on the `google` provider
  ([task models](task-models.spec.md) 3.1).
- **Delivery** (`integrations/nanoclaw/argus-accounts.ts`,
  `integrations/nanoclaw/argus-agy-stub.ts`): generic OneCLI secrets per agent
  group, distinct per auth mode, with no broad host wildcards:
  - **API key mode**: `argus-<group>-google-gemini` on host
    `generativelanguage.googleapis.com`, path `/v1beta/models*`, injecting
    header `x-goog-api-key: {value}`. The trusted host gives the worker the
    non-secret sentinel `GEMINI_API_KEY` and strips any OAuth stub.
  - **OAuth subscription mode**: three generic secrets with header
    `Authorization: Bearer {value}`:
    - `argus-<group>-google-userinfo` on `www.googleapis.com`, path
      `/oauth2/v2/userinfo`
    - `argus-<group>-google-cloudcode-daily` on
      `daily-cloudcode-pa.googleapis.com`, path `/v1internal*`
    - `argus-<group>-google-cloudcode` on `cloudcode-pa.googleapis.com`, path
      `/v1internal*`
    The trusted host mounts a read-only sentinel stub at
    `/home/node/.gemini/antigravity-cli/antigravity-oauth-token` (fake
    `access_token`, fake `refresh_token`, far-future `expiry` `2099-01-01T00:00:00Z`
    so the CLI never attempts a refresh inside the container) and strips
    `GEMINI_API_KEY`.
  - In selective mode `reconcileSecretAssignments` keeps only the secrets of
    currently held leases, so switching between API-key and OAuth `google`
    accounts unassigns the previous mode's secrets cleanly.
- **Child runtime** (`argus-delegate agy`):
  - `authStatus` accepts only the gateway sentinel `GEMINI_API_KEY` (API key
    mode) or a present `antigravity-oauth-token` stub (OAuth mode). Missing
    both exits `78` with `{"blocked":"auth_missing"}`.
  - `prepareAgyEnv` creates a per-run private `HOME` (`0700`) under
    `{authorized cwd}/.argus-delegate-runtime/agy-home/<uuid>/`, removed when
    the delegate exits, with `NO_BROWSER=1`. In OAuth mode it copies the
    read-only sentinel stub into the private `HOME` (`0600`) and scrubs
    `GEMINI_API_KEY`; in API key mode it keeps only the sentinel
    `GEMINI_API_KEY` and writes no OAuth file.
  - **Invocation**:
    - API key mode adds `--settings '{"modelProvider":"gemini"}'`.
    - Always passes `--output-format stream-json` and `-p <prompt>` (prompt via
      argv).
    - Read-only delegation passes `--mode plan` (and omits
      `--disable-slash-commands`, because `agy 1.5.12` warns that `--mode plan`
      has no effect when slash command expansion is disabled).
    - Write mode passes `--dangerously-skip-permissions` and
      `--disable-slash-commands`.
    - Explicit `--model <id>` and `--effort <low|medium|high|xhigh|max>` are
      forwarded when set.
  - **Auth probe** (`argus-delegate agy --auth-probe`): runs credential-presence
    and `agy --version` checks, then makes one read-only metadata request
    through the gateway: `GET https://generativelanguage.googleapis.com/v1beta/models`
    (`x-goog-api-key`) in API key mode, or
    `GET https://www.googleapis.com/oauth2/v2/userinfo` (`Authorization: Bearer`)
    in OAuth mode.
- **Usage & live log**:
  - `parseUsage("agy", stdout)` reads the final `{"event":"result"}` event's
    `result.usage` (`input_tokens`, `output_tokens`, `cache_read_tokens`),
    verified against `agy 1.5.12` (where `input_tokens` already excludes
    `cache_read_tokens` and `output_tokens` already includes `thinking_tokens`),
    plus `init.model` and `result.num_turns`. Failed zero-token runs report
    `not_reported`.
  - `livelog.mjs` streams `init`, `step_update` (`agent_response` `text_delta`
    and tool calls), and `result` events.
- **Failure classes**:
  - `agyAuthRejected` detects `UNAUTHENTICATED` / HTTP 401 and 403 in
    `AGY_ERROR` stderr JSON or `result.error` (`Eligibility check failed:
    UNAUTHENTICATED (code 401)` / `Error 401, Message: API key not valid`) and
    queues `account.auth_rejected` to the host with a fixed reason.
  - `QUOTA_NOTICES` (`packages/core/src/argus_core/quota.py` and
    `integrations/nanoclaw/argus-accounts.ts`) classifies Google `HTTP 402` /
    `HTTP 429` / `RESOURCE_EXHAUSTED` (including `"prepayment credits are
    depleted"` and `"exhausted your capacity"`) as `quota_exhausted`.
- **Quota** ([account usage](account-usage.spec.md) 3.3): no documented
  per-account quota endpoint is polled; a `google` account reads `unknown` with
  reason `no_provider_source`, never `0`.
- **Engine catalogue** (public API): Antigravity (`id: "antigravity"`,
  `executor: "agy"`, `account_provider: "google"`) is `role: executor` only,
  default-on in the agent image via `install-agy.sh` (pinned `1.5.12` tarball,
  SHA-512 verified for both `x64` and `arm64`).

# 4. Open Questions

- Whether a dedicated Argus-driven Google OAuth login wizard (like Codex/Claude)
  is worth adding once OAuth subscription runs are observed in production.
- Whether enterprise Google Cloud / Vertex endpoints
  (`businessaicode.googleapis.com`, regional Vertex hosts) should be added
  alongside consumer Cloud Code and Gemini API endpoints.

# 5. Constraints

- Admin scope for import and credential replace. Workers lease only; the
  container never sees a real API key, OAuth access token or refresh token.
- Lease apply runs on the NanoClaw host, not in the worker container.
- No credential value in a response, listing, event, log, error, or delegate
  report.
- No network call inside a write transaction.

# 6. Implementation Consequences

- `packages/core`: `google_auth.py`; `accounts.py` (`EXECUTOR_ONLY`);
  `tasks.py` (`EXECUTOR_PROVIDERS`, `ExecutorModel`); `difficulty.py`;
  `model_trace.py`; `quota.py`; `execution_chains.py` (`agy`/`google` selector
  and effort levels).
- `apps/api`: `google` validation on create/replace; `accounts/google_refresh.py`
  pre-lease OAuth refresh; auth-rejection reason in `accounts/service.py`;
  `antigravity` entry in `engines/catalog.py` and `engines/image_tools.py`;
  regenerated OpenAPI client (`make openapi`).
- `integrations/nanoclaw`: `argus-accounts.ts` (API key + OAuth mappings),
  `argus-agy-stub.ts` (sentinel env / OAuth stub mount), `install.sh` (`onecli.ts`
  hook and `CODEX_ENV_ALLOWLIST`), `argus.ts` (`renderTask`),
  `engines/install-agy.sh` (SHA-512 pinned `1.5.12` installer),
  `engines/patch-engines-dockerfile.py`, `engines/install-engines.sh`,
  `engines/smoke.sh`, `engines/argus-delegate.mjs`, `engines/livelog.mjs`,
  `engines/usage.mjs`, `engines/model-policy.mjs`, `transcripts-derive.ts`, and
  recorded stream fixture `engines/fixtures/agy-stream.jsonl`.
- Dashboard: `accountProviders.ts` (`GOOGLE_PROVIDER`, `googleCredentialNotice`),
  `providerIcons.tsx` (`google` mark, `agy`/`antigravity`/`google`/`gemini`
  mapping), `AccountDialogs.tsx` (add and replace dialogs).
- **Acceptance (automated)**: `make check`; channel/stub/delegate/parser/installer
  unit tests with fake credentials and the recorded `agy 1.5.12` fixture (no
  live Google call in CI).
- **Acceptance (fleet)**: parent-run `argus-delegate agy --read-only` with an
  explicit model, native CLI exit code `0`, and a controlled nonce echoed in the
  CLI JSON result.

## Verification boundary

- Commands:
  - `uv run pytest -q -p no:cacheprovider packages/core/tests/test_google_auth.py apps/api/tests/test_google_accounts.py`
  - `node --test integrations/nanoclaw/engines/argus-delegate.test.mjs integrations/nanoclaw/engines/usage.test.mjs integrations/nanoclaw/engines/livelog.test.mjs`
  - `pnpm --filter @argus/dashboard exec vitest run src/views/AccountsGoogle.test.tsx src/views/Accounts.test.tsx`
  - `make check`
- Verified: unit, fake-API, and offline real-binary proxy probe level against
  `agy 1.5.12` (verified `x-goog-api-key` on `generativelanguage.googleapis.com`,
  sentinel `antigravity-oauth-token` on `www.googleapis.com` and
  `daily-cloudcode-pa.googleapis.com`, and `stream-json` usage/error output).
- Not verified: a live paid Gemini API or Google OAuth subscription call; an
  agent Docker image build running `install-agy.sh` and `smoke.sh`; a real
  OneCLI gateway swap in a live worker container; any production node.
