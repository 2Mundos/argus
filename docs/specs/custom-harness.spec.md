# Custom OpenAI-compatible harness (NanoClaw engine)

Builds on [custom accounts](custom-accounts.spec.md), [local Strata harness](local-harness.spec.md),
[engines](engines.spec.md) and [global account selection](account-selection.spec.md).

# 1. Purpose and Scope

Run the NanoClaw **Claude Code** harness against a leased `custom` account (first case
OpenRouter) while keeping the **executor** independent (typically Cursor `agent` via
`argus-delegate`). Claude Code speaks the Anthropic Messages API only; the worker never
calls the provider's OpenAI-compatible URL directly. Instead the trusted host runs a
loopback **bridge** that translates Messages ↔ Chat Completions and forwards to the
account's configured `base_url` with the leased API key held only in host memory.

# 2. Non-Goals

- Codex as the harness on a custom account (Claude Code is the supported harness).
- Using OpenRouter as the executor in this flow (delegation stays on Cursor or another
  executor provider).
- Storing the API key in harness files, container env, prompts or logs.
- Calling any host other than the account's configured `base_url` (no redirects followed).
- Replacing the egress proxy or relaxing SSRF rules for cloud Anthropic traffic.

# 3. Decisions Made

- **Provider mapping.** `custom` in `integrations/nanoclaw/argus-accounts.ts` maps to
  NanoClaw engine `claude` with `harnessEnvOnly: true` (no OneCLI secret for the engine
  lease). The executor lease is unchanged: Cursor keeps path-scoped secrets on
  `api2.cursor.sh`.
- **Lease `settings`.** A custom engine lease carries `settings` (`base_url`, `label`,
  `model`, `provider` block) from Argus. The channel copies the engine role's settings
  into the pinned model-policy context as `engine_settings` and the executor role's as
  `executor_settings` (delegate spec). Secrets never appear in either field.
- **Bridge.** `argus-custom-bridge` on the NanoClaw host listens on loopback only,
  starts when a custom engine lease is applied, and stops when the lease is released or
  replaced. It receives the API key and `settings` only in process memory from the
  channel at apply time. Outbound requests use `Authorization: Bearer`, the configured
  `model`, and the OpenRouter `provider` block in the JSON body when present
  (`chat_body` rule in core).
- **Container path.** Same redirect pattern as local Strata: `ANTHROPIC_BASE_URL` points
  at `http://host.docker.internal:<port>` (bridge), `ANTHROPIC_AUTH_TOKEN` is the public
  placeholder `argus-custom-harness`, and `ARGUS_ALLOWED_ANTHROPIC_BASE_URL` matches.
  `anthropic-redirect.mjs` allows only loopback / `host.docker.internal` bases with no
  cloud credential env present. A TCP forwarder on the Docker bridge (reuse
  `argus-local-forward.ts`) exposes the bridge port to the agent container.
- **Harness file.** `argus-custom-harness.json` under the agent group directory holds
  only non-secret bridge coordinates (`anthropic_base_url`, optional display `model`);
  the host merges it in `withCustomHarnessRuntime` like the local harness file.
- **Model precedence.** The bridge always sends the account's role `model` from settings
  when set; otherwise the model from the incoming Anthropic request. Policy harness
  chains still apply to `ANTHROPIC_MODEL` for Claude Code's own model label when useful.
- **Quota and auth.** HTTP 402/429 from the upstream are turned into failure notices that
  match the `custom` quota patterns shared with Argus (`quota-notices.json`). Invalid key
  (401) exhausts the account like other auth failures. Network errors are ordinary task
  failures, not quota.
- **Observability.** `POST /engines/selected` records `provider: custom` and
  `engine: claude`. Model-policy context carries `engine_settings` / `executor_settings`
  (no secrets) so `argus-delegate` reports executor model and label separately from the
  harness account.
- **Mutual exclusion.** At most one of local Strata harness or custom bridge harness is
  active per agent group; applying one removes the other's files and forwarder.

# 4. Open Questions

- Whether to record bridge-reported upstream model ids on harness executions when the
  provider returns them (model observability spec).
- Hard-task policy pinning cloud-only engines when a custom account is cheapest.

# 5. Constraints

- Single-node spec; the bridge is an in-process HTTP server on the NanoClaw host, not a
  new required service.
- `check_base_url` / URL rule from custom accounts: bridge outbound URL must match the
  leased `base_url` host and path prefix `/v1`.
- Spec-first: behaviour changes update this file and [custom accounts](custom-accounts.spec.md).

# 6. Implementation Consequences

- `integrations/nanoclaw/engines/anthropic-openai-bridge.mjs` and tests.
- `integrations/nanoclaw/argus-custom-harness.ts`, `argus-custom-bridge.ts`, tests.
- `argus-accounts.ts` (`custom` mapping, `runtime_providers`, quota patterns),
  `argus.ts` (lease settings, bridge lifecycle, model-policy context),
  `argus-local-harness.ts` (combined runtime wrapper), `install.sh`.
- `packages/core/tests/fixtures/quota-notices.json` custom rows; channel quota test
  passes `custom` provider into `classifyFailure`.
- [engines.spec.md](engines.spec.md) and [custom-accounts.spec.md](custom-accounts.spec.md)
  NanoClaw harness sections updated to match.
