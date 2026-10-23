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
  executor provider). In particular a Claude Code executor never runs on the bridge.
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
- **Bridge.** `argus-custom-bridge` on the NanoClaw host listens where agent containers
  reach it (next item) and nowhere else, starts when a custom engine lease is applied, and stops when the lease is released or
  replaced. It receives the API key and `settings` only in process memory from the
  channel at apply time. Outbound requests use `Authorization: Bearer`, the configured
  `model`, and the OpenRouter `provider` block in the JSON body when present
  (`chat_body` rule in core).
- **Container path.** Same redirect pattern as local Strata: `ANTHROPIC_BASE_URL` points
  at `http://host.docker.internal:<port>` (bridge), `ANTHROPIC_AUTH_TOKEN` is the public
  placeholder `argus-custom-harness`, and `ARGUS_ALLOWED_ANTHROPIC_BASE_URL` matches.
  `anthropic-redirect.mjs` allows only loopback / `host.docker.internal` bases with no
  cloud credential env present.
- **Bridge reachable from agent containers.** Agent containers run on Docker's default
  bridge and resolve `host.docker.internal` to its gateway (docker0). Where the bridge
  listens depends on where NanoClaw runs:
  - *NanoClaw in a fleet container* (the Docker target with the host socket): that
    container is on its own network (e.g. `onecli-control`), so neither its loopback
    nor its IP is reachable from agent containers (Docker isolates bridge networks).
    The fleet publishes container port `3141/tcp` on the default bridge gateway at a
    free host port and sets `ARGUS_CUSTOM_BRIDGE_PORT` / `ARGUS_CUSTOM_BRIDGE_CONTAINER`;
    the bridge listens on `0.0.0.0:3141` inside the container and the container URL
    names the published host port (`docker port`), i.e.
    `http://host.docker.internal:<published port>`. Published on the gateway address,
    the port is reachable from containers and the Docker host, not from the LAN. A start
    that cannot resolve the published port fails the apply (no harness) instead of
    handing the container an unreachable URL. A daemon without a default bridge
    gateway fails the NanoClaw container's start (its agents could not run either).
    The port is fixed per container, so one NanoClaw container runs at most one custom
    harness at a time (a fleet instance has one agent group); a second one fails to
    bind with a clear error. Containers created before this change have no published
    port: replace them (recycle) when deploying.
  - *NanoClaw on the Docker host* (no such env): loopback bridge plus the docker0 TCP
    forwarder the Strata path uses (`argus-local-forward.ts`). A sandboxed fleet pool
    (inner dockerd, no host socket) is this case too: its agents' host.docker.internal
    is the sandbox's own docker0, where NanoClaw itself runs.
  - The bridge lives in the NanoClaw process; after a restart a leftover harness file
    is ignored until a lease re-applies it (no URL or key of a dead bridge is handed on).
  - Inside a fleet container the bridge listens on 0.0.0.0, so containers on that
    container's own network (e.g. `onecli-control`) can reach it too; the bridge key is
    what guards it there.
  - Either way the bridge is reachable by any container on the default bridge, so each
    bridge has a random key: requests without `x-argus-bridge-key: <key>` get 401. The
    key reaches Claude Code as `ANTHROPIC_CUSTOM_HEADERS` and is stored in the harness
    file; it guards only that bridge and dies with the lease.
  - The real-Docker integration test (`make test-docker`) starts the bridge the way
    the fleet starts a NanoClaw host and proves an agent-network container reaches it
    with the key and is refused without it.
- **Harness file.** `argus-custom-harness.json` under the agent group directory holds
  only bridge coordinates (`anthropic_base_url`, optional display `model`, the per-bridge
  `bridge_key`; never the account's API key);
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
- **Harness orchestrates only; the executor never rides the bridge (2026-10-21).** The
  custom account serves only the NanoClaw harness. The smoke run 7bc671b6 showed the gap:
  the engine lease was OpenRouter and the executor lease the best Anthropic account, but
  `withCustomHarnessRuntime` had removed the Claude gateway stub and left
  `ANTHROPIC_BASE_URL` (bridge), `ANTHROPIC_AUTH_TOKEN` (`argus-custom-harness`) and
  `ANTHROPIC_CUSTOM_HEADERS` in the container env. `argus-delegate claude` inherited them,
  so its "claude-sonnet-5-5" run went through the bridge and ran on the custom model
  (the bridge always sends the account's model). Cursor `agent` was blocked with
  `auth_missing` because no Cursor account was leased, so no Cursor stub was mounted. So:
  - *Marker.* The custom harness env carries `ARGUS_CUSTOM_HARNESS=1` (a redirect with the
    `argus-custom-harness` placeholder counts too, for older envs).
  - *Delegate env.* On a custom harness `argus-delegate` removes the bridge variables
    (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ARGUS_ALLOWED_ANTHROPIC_BASE_URL`,
    `ANTHROPIC_MODEL`, `ANTHROPIC_CUSTOM_HEADERS`, `ARGUS_CUSTOM_HARNESS`) from every
    executor's environment before anything else; no executor reaches the bridge or sees
    its key.
  - *Claude Code executor only on a real Anthropic account.* `withCustomHarnessRuntime`
    keeps the Claude gateway stub (it is written only when OneCLI issued an Anthropic
    sentinel for the group, i.e. an Anthropic executor lease is held; the harness itself
    does not read it). `argus-delegate claude` on a custom harness runs only when the stub
    restores the sentinel through the gateway (`auth_source: gateway_sentinel`); otherwise
    it refuses (`auth_missing`, reason `custom_harness_engine_only`) and nothing runs.
  - *Cursor first.* On a custom or local engine lease the channel leases the executor
    with `runtime_providers: ["cursor"]` first, so the Cursor stub is mounted, and falls
    back to the ordinary executor lease (any provider) only on `no_account`. Acquisition
    order is unchanged; the *selected lease* then decides the CLI (below).
  - *Prompt.* The task message adds the custom harness rules: orchestrate, delegate
    implementation, gates and review to `argus-delegate EXECUTOR` of the executor
    actually leased (Cursor `agent`, or the fallback `claude` or `codex`); `claude`
    never runs on the harness's account. The preferred line, the harness rules, trivial
    text and command example all name that one CLI, and a task pin overrides it. Cursor
    is never named when it was not leased. With no executor leased the prompt says
    delegation is blocked for lack of an account or capacity (not invalid
    credentials) and tells the harness not to probe an unleased executor.
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
- `apps/api/src/argus_api/fleet/runtimes.py` / `targets.py`: the published bridge port
  (`ContainerSpec.ports`, `HOST_GATEWAY`); `test_docker_integration.py` covers the path.
- `packages/core/tests/fixtures/quota-notices.json` custom rows; channel quota test
  passes `custom` provider into `classifyFailure`.
- [engines.spec.md](engines.spec.md) and [custom-accounts.spec.md](custom-accounts.spec.md)
  NanoClaw harness sections updated to match.
