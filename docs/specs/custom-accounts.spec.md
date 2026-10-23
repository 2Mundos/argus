# Custom OpenAI-compatible accounts

Builds on [global account selection](account-selection.spec.md), the account pool in
[worker lifecycle 3.9](worker-lifecycle.spec.md), [engines](engines.spec.md) and
[preferred models](model-selection.spec.md). Single-node rules apply
([spec](single-node.spec.md)): no new service, no new secret store.

# 1. Purpose and Scope

Make an OpenAI-compatible endpoint (first case OpenRouter,
`https://openrouter.ai/api/v1`) a normal Argus **account** with provider `custom`:

- **Registration**: `base_url`, a provider `label` (for example `openrouter`) and an
  API key. The key is the account's only secret and lives in the SOPS store as
  `{"api_key": …}` like every other account. `base_url`, `label` and the settings
  below are plain configuration, stored in `accounts.config` (JSON).
- **Per account and per role**: `engine` (the NanoClaw harness) and `executor` (the
  code executor) each carry a free-text `model` and, for `label = openrouter`,
  `routing_options` (`order`, `only`, `quantizations`, `allow_fallbacks`).
- **Role availability**: a role with no `model` is unavailable on that account, and
  only that role: an account with an `engine` model and no `executor` model serves
  the harness, never a delegated CLI, in the ranking, the lease, executor reuse of
  the held engine lease, `earliest_reset` and the engine pool alike. An account with
  no model in either role, or without a config, serves neither. Native providers
  carry no config and are not restricted.
- **Readiness**: `POST /accounts/{id}/probe` calls `GET {base_url}/models` with the key
  as bearer and returns the model ids as suggestions for the `model` field.
- **Routing choices**: `GET /accounts/{id}/models/{author}/{slug}/endpoints` proxies
  OpenRouter's `models/{author}/{slug}/endpoints` (host, quantization, context, price)
  so the dashboard can offer real hosts for `order` and `only`.
- **Lease**: a lease of a custom account returns `settings` (`base_url`, `label`,
  `model`, `provider`) for the leased role next to the credential. `provider` is the
  request-body block built from `routing_options`.
- **Failover**: HTTP 402 and 429 from the provider are quota; the account is marked
  exhausted and the agent moves to the next account without burning an attempt.
- **Task view**: the serving account on an attempt shows the configured `model` and
  the `host` of a custom account.

# 2. Non-Goals

- Making Claude Code speak an OpenAI endpoint. It cannot; see 3 (engine use).
- Provider-observed model ids (still unknown, see [preferred models](model-selection.spec.md)).
- Routing options for labels other than `openrouter`; they are rejected, not ignored.
- Spend caps, balance display, or following redirects to another host.
- Any other service dependency, or a second secret mechanism.

# 3. Decisions Made

- **Provider and credential**: provider `custom`, kind `api_key`. The credential is
  exactly `{api_key}`: a non-empty string, no surrounding whitespace, no CR/LF/NUL.
  Errors name fields, never values. `AccountCreate.config` is required for custom
  accounts and rejected for others; `PATCH` may replace `config` of a custom account.
  The key is never returned by any GET, list, event or log; FastAPI's 422 already omits
  the offending input.
- **One URL rule**: `check_base_url` in `argus_core.custom_accounts` (http(s), a host,
  no credentials, no query or fragment, trailing slash dropped) is the rule behind
  both `engines/local.py::validate_url` and `base_url`. Custom errors do not echo the
  URL, since it could carry a secret.
- **Routing options**: `order`, `only` and `quantizations` accept a list or a
  comma-separated string and drop empties. `provider_block()` is a port of Radar's
  `openrouter_provider_block_from_preferences`: `allow_fallbacks=false` with an
  `order` and no `only` pins `only` to `order`; an empty result is `None`. The block
  goes into the request body as `"provider": {…}` (`chat_body`).
- **Probe**: admin only. Redirects are not followed, 15 s timeout, and the answer is a
  fixed vocabulary (`ok`, `unauthorized`, `exhausted`, `unreachable`, `failed`); no
  upstream text. It reports and changes nothing. It is separate from `QuotaProber`,
  which runs a Codex subprocess.
- **Endpoints proxy**: manager scope, `openrouter` label only, `author` and `slug`
  validated as one catalogue path segment each, no key sent (public data), no
  redirects. Failures are `502 upstream_error`, or `404` when the model is unknown.
- **Quota**: no new mechanism. `quota_notice_from_response(status, headers, now)`
  turns 402/429 into the canonical notice `HTTP 429 rate limited (resets <ISO>)`.
  The reset comes from `Retry-After` (seconds or date) else `X-RateLimit-Reset`
  (epoch seconds or milliseconds); the body is never used. The `custom` pattern in
  `QUOTA_NOTICES` classifies it, and `fail_task` and `POST /accounts/{id}/exhausted`
  take it from there as for any provider. Runtimes report the notice, not the body.
- **Effective model** for a custom account, first match wins: an explicit
  `--model`; the account's role `model`; the [model policy](model-selection.spec.md)
  chain of the engine; the executor's native default.
- **Engine use**:
  - **Delegate** (`integrations/nanoclaw/engines/argus-delegate.mjs`): lease `settings`
    reach it as `executor_settings` in the pinned task context or as the JSON in
    `ARGUS_ACCOUNT_SETTINGS`; neither holds a secret. The key is read only from the
    environment variable `ARGUS_ACCOUNT_API_KEY`, never from argv, a file or the report.
    - **OpenCode** gets `base_url`, `model` and the `provider` block through an inline
      config in `OPENCODE_CONFIG_CONTENT` (an `@ai-sdk/openai-compatible` provider
      `argus_custom`, `-m argus_custom/<model>`; the block sits in the model's
      `options.provider`). The config names the key as `{env:ARGUS_ACCOUNT_API_KEY}`.
    - **Codex** gets `base_url` and `model` through `-c model_providers.argus_custom.*`
      (`env_key = "ARGUS_ACCOUNT_API_KEY"`) and `-m`. Codex **cannot carry `provider`**:
      it cannot add body fields, so the block is dropped and the report says
      `provider_block_applied: false`.
    - `claude` and `agent` cannot use a custom account: the delegate exits 64.
    - A missing key is `78 auth_missing`. If an executor echoes the key, the report's
      `tail`, `stderr_tail` and `executor_error` have it replaced by `[redacted]`
      (the raw artifact files are the executor's own output and are not rewritten).
    - The report names `model` and `model_source` (`cli`, `account`, `policy`,
      `native_default`), plus `account_label` and `account_base_url` (no credentials by
      the URL rule).
  - **NanoClaw harness**: see [custom harness](custom-harness.spec.md). The engine
    lease uses a host loopback bridge (Anthropic Messages → OpenAI Chat Completions);
    the API key never enters the container. The channel copies lease `settings` into
    `engine_settings` / `executor_settings` on the pinned model-policy context.
    `PROVIDERS.custom` is `harnessEnvOnly`; the executor lease stays separate (Cursor,
    Codex, …).
  - **Claude Code** cannot call an OpenAI-compatible URL directly; the bridge is the
    only supported harness path for `custom` engine accounts.

# 4. Open Questions
- `/models` is the only suggestion source; a gateway without it gets free text.

# 5. Constraints

- The API key reaches only the account's own `base_url`; the endpoints proxy sends none.
- No network call inside a write transaction; the probe and proxy run outside one.
- Model ids match `^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$`; labels `^[a-z0-9][a-z0-9_-]{0,49}$`.
- `serving_account.model` is the configured model, not what the provider served.

# 6. Implementation Consequences

- `packages/core/src/argus_core/custom_accounts.py`: models, parser, `chat_body`,
  endpoints parsing; `quota.py`: custom pattern and `quota_notice_from_response`.
- Migration `0027_account_config.sql` adds `accounts.config`.
- `apps/api/src/argus_api/accounts/custom.py`: probe and endpoints proxy; routes in
  `accounts/routes.py`; lease `settings` in `accounts/service.py`; test transport via
  `app.state.custom_transport`.
- Dashboard: the custom account form and the model and host on the attempt.
- Delegate: `argus-delegate.mjs` (`parseAccountSettings`, `effectiveModel`,
  `applySettingsEnv`, Codex `-c` args), tests in `argus-delegate.test.mjs`.
- Tests: `packages/core/tests/test_custom_accounts.py`,
  `apps/api/tests/test_custom_accounts.py` (in-process fake provider).
