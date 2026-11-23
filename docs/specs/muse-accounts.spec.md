---
status: partial
created: 2026-11-23
updated: 2026-11-23
---

# Muse accounts: import, harness and executor lease, delegated CLI

## Open items

- A worker loop on a leased Muse account in a real agent image, through a
  real OneCLI gateway swap (fleet smoke, §6 acceptance).
- An agent image build running `engines/install-muse.sh` and `smoke.sh`.
- Narrowing the gateway secret from host-wide (`/*`) to exact API paths, and
  recording auth-rejection text and rate-limit signals so a rejected or
  exhausted Muse account rotates.
- A usage parser and live-log events for Muse.
- Shell commands in a delegated `muse exec`: it keeps its Bubblewrap sandbox,
  which Docker's default profile denies in a worker container (§4).

Builds on [engines](engines.spec.md), the account pool in
[worker lifecycle 3.9](worker-lifecycle.spec.md) and the executor pattern of
[Cursor accounts](cursor-accounts.spec.md) and
[Copilot accounts](copilot-accounts.spec.md). Single-node rules apply
([spec](single-node.spec.md)): no new service; reuse the existing `engine`
and `executor` lease roles.

# 1. Purpose and Scope

Make a Muse credential (a `META_API_KEY` API key or an imported `muse login`
Meta account token) a normal Argus **account**, validate its shape on create
and replace, and run it both as a NanoClaw **harness** and as a delegated
executor (`argus-delegate muse`):

- **Import**: the operator stores `{"api_key": …}` (`kind: "api_key"`),
  `{"oauth_token": …}` (`kind: "subscription"`), or a pasted `muse` auth
  file (`{"providers": {"meta": {"access_token": …}}}`, the shape the `muse`
  launcher itself reads at `$MUSE_AUTH_PATH`, default
  `~/.config/muse/auth.json`; `kind: "subscription"`). Extra launcher
  metadata is tolerated and ignored. Errors name fields and rules only,
  never values.
- **Lease**: the `engine` lease role may select a `muse` account (harness),
  and the `executor` lease role may select one next to the engine's account.
  Muse is the first engine besides Claude and Codex that can run the worker
  loop.
- **Delivery**: the NanoClaw channel (trusted host) writes the credential
  into the OneCLI vault. The worker receives it as `META_API_KEY`, the
  environment variable the CLI documents as taking priority over the account
  login. The real key or token never enters the image, checkout or a log.
- **Executor selection**: the executor lease is the best usable account
  across all runtime-supported providers by priority
  ([global account selection](account-selection.spec.md)); a Muse account is
  leased as the executor when it ranks first or when an execution chain or
  task pin selects `muse`.

# 2. Non-Goals

- Reading host `~/.config/muse`, the OS keychain, live Argus secrets or
  `.env` from agents. Import is something the operator does, naming the
  credential; Argus never looks in those places.
- A dedicated interactive login wizard driven by Argus (the Codex and Claude
  device-code pattern). v1 is import only: the operator runs `muse login` or
  mints a key where they are authorised to, then stores the result in Argus.
- Refreshing Muse OAuth tokens inside the NanoClaw channel or worker
  container; operators renew in Argus when the CLI reports the credential
  rejected (same posture as Cursor).
- Reading Muse quota or billing from an undocumented endpoint: a `muse`
  account reads `unknown` with reason `no_provider_source`, never 0.
- Running a paid Muse model call in CI; fleet acceptance is a parent-run
  bounded smoke with a real leased account.

# 3. Decisions Made

- **Account shape** (`packages/core/src/argus_core/muse_auth.py`):
  `provider: "muse"`, mutually exclusive shapes, never mixed:
  `{"api_key": …}` (`kind: "api_key"`), `{"oauth_token": …}` or a pasted
  `muse` auth file (`kind: "subscription"`). Values are non-empty printable
  strings without whitespace; no prefix is enforced because the Meta token
  format is not verified. Responses and events never echo any value.
- **Harness and executor provider**: `muse` joins `anthropic` and `openai`
  in `ENGINE_PROVIDERS` (never in `EXECUTOR_ONLY`); the `engine` lease role
  may return it and the `executor` role may. The `muse` executor runs on the
  `muse` provider ([task models](task-models.spec.md) 3.1): an executor pin
  to it with no usable muse account is refused with a missing-provider
  reason, never leased from another provider.
- **Delivery** (`integrations/nanoclaw/argus-accounts.ts`,
  `integrations/nanoclaw/argus-muse-stub.ts`): one generic OneCLI secret per
  agent group (`argus-<group>-muse-api`) on the Meta API host, holding the
  picked credential (`api_key`, `oauth_token`, or the auth file's
  `access_token`). The worker holds only the non-secret sentinel
  `META_API_KEY` (the variable the CLI documents as taking priority over
  the account login, so no other variable or login in the container can
  supply a different credential), and the gateway injects the real value on
  the wire. Muse 1.4.25 sends the key as `Authorization: Bearer`, reaches
  `api.meta.ai:443` through `HTTPS_PROXY`, and trusts the gateway CA through
  `SSL_CERT_FILE`. The path pattern is host-wide (`/*`) until the exact API
  paths are recorded (§4). A missing lease is `78 auth_missing`, never a
  silent fallback.
- **Harness runtime** (`integrations/nanoclaw/overlays/muse-provider.ts`):
  the runner drives `muse acp` (Agent Client Protocol over stdio) with an
  explicit model. As for the Claude, Codex and OpenCode harnesses, the worker
  container and the OneCLI allow-list are the boundary: `--disable-sandbox`,
  because Docker's default profiles deny Muse's Bubblewrap, and tool
  permission requests are allowed once. `--trust-workspace` covers the group
  directory only; without it Muse neither loads the composed `CLAUDE.md` nor
  starts the MCP servers given in `session/new`. Runner instructions and
  memory open each new session. Sessions persist in a per-group directory
  (`XDG_DATA_HOME`, `overlays/muse-host.ts`); a stored session the host no
  longer holds (`-32020`) becomes a new session and the message is kept.
- **Default model**: `muse-spark-1.3` for the harness and for implementation,
  so a worker switching to Muse never keeps the previous engine's model.
- **Usage and live log**: `parseUsage("muse", …)` reports `not_reported` and
  the live log has no muse event parser until usage fields are verified on a
  real stream (§4). Accounting still spools the delegate report; figures are
  absent, never zero.
- **Child runtime** (`argus-delegate muse`):
  `authStatus` accepts only the gateway sentinel in `META_API_KEY`. Invocation
  is `muse exec --json --workspace <cwd> --model <explicit>` with the prompt
  in a private `--prompt-file`, so its size is not bound by the argv limit.
  The model is always explicit, never the CLI default. Read-only delegation
  adds `--disable-write --disable-shell`; write mode keeps the workspace
  sandbox on and never passes `--yolo`/`--disable-approval`.
- **Engine catalogue** (public API): Muse (`id: "muse"`) has both layers
  (`harness` and `executor`), like Claude and Codex. Readiness follows the
  usual ledger-plus-registry rule; `usable` needs one available account, and
  `authenticated` comes from the registry
  (the catalogue rule in [Codex accounts](codex-accounts.spec.md)).
- **Routing**: `muse` joins `claude` and `codex` in the harness ranking; the
  highest-priority usable engine runs the task, and the preferred code
  executor is the best usable harness CLI that is not the engine itself.

# 4. Open Questions

- The exact Meta token format for `oauth_token` (no prefix is enforced until
  a real login token is observed) and the exact Meta API request paths the
  CLI calls: v1 scopes the gateway secret by host (`api.meta.ai`, the only
  host a real run contacted) until the paths are recorded for a Cursor-style
  path-scoped secret.
- How a delegated `muse exec` should run shell commands in a worker
  container, where Docker's default profile denies its Bubblewrap sandbox.
  The Codex sandbox profile excludes other providers and has not passed a
  runtime test, so delegated Muse keeps its sandbox flags for now.
- Whether a dedicated Argus-driven Muse login flow (like Codex/Claude) is
  worth adding once Muse harness runs are observed in production.
- The CLI's exit codes and error text for auth rejection and rate limits;
  until verified the delegate treats every non-zero exit as an ordinary
  failure (no account exhaustion from delegate output).
- The usage fields of a real `muse exec --json` stream: the record envelope
  is known from the echo provider, but usage figures and the live-log parser
  stay unverified until a real stream is recorded.
- An agent image build running `install-muse.sh` and `smoke.sh` (the
  installer is verified below only for checksum pinning and script shape).

# 5. Constraints

- Admin scope for import and credential replace. Workers lease only; the
  container never sees a refresh token or any credential but the leased one.
- Lease apply runs on the NanoClaw host, not in the worker container.
- No credential value in a response, listing, event, log, error, or delegate
  report.
- No network call inside a write transaction.

# 6. Implementation Consequences

- `packages/core`: `muse_auth.py`; `accounts.py` (`ENGINE_PROVIDERS`);
  `tasks.py` (`HARNESS_PROVIDERS`, `EXECUTOR_PROVIDERS`, harness/executor
  literals); `difficulty.py` if the executor joins difficulty chains;
  `execution_chains.py` (`muse`/`muse` selectors and effort levels).
- `apps/api`: `muse` validation on create/replace; `muse` entry in
  `engines/catalog.py` (both layers) and `engines/image_tools.py`;
  `engines/router.py` harness ranking; regenerated OpenAPI client
  (`make openapi`).
- `integrations/nanoclaw`: `argus-accounts.ts` (`muse` mapping, engine
  `muse`, `EXECUTOR_BY_PROVIDER`), `argus-muse-stub.ts` (sentinel
  `META_API_KEY` runtime), `overlays/muse-provider.ts` and
  `overlays/muse-host.ts` (harness, installed by `install-engines.sh`),
  `argus.ts` (harness selection), `install.sh` (stub hook), `argus-delegate`
  (`muse` subcommand, private `HOME`, explicit model, `not_reported` usage),
  `model-policy.defaults.json` (default model), `smoke.sh` (version and auth
  rows), `engines/install-muse.sh` (pinned `1.4.25-R9361.1`, SHA-256 per
  arch, verified against Meta's release manifest), and
  `patch-engines-dockerfile.py` (installer step and version smoke).
- Dashboard: `accountProviders.ts` (`MUSE_PROVIDER`, credential notice) and
  `AccountDialogs.tsx` (add and replace dialogs).
- **Acceptance (automated)**: `make check` and the NanoClaw release build;
  channel, delegate, parser and ACP provider tests with fake credentials and
  a fake agent (no live Muse call in CI).
- **Acceptance (fleet)**: parent-run `argus-delegate muse --read-only` with
  an explicit model, native CLI exit code `0`, and a controlled nonce echoed
  in the CLI result; plus one harness run of a worker on a leased muse
  account completing a trivial task. Record outcomes in a dated ref when
  verified.

## Verification boundary

- Commands:
  - `uv run pytest -q -p no:cacheprovider packages/core/tests/test_muse_auth.py`
  - `node --test integrations/nanoclaw/engines/argus-delegate.test.mjs`
  - `deploy/nanoclaw-host/build-release.sh <tag>` runs the channel and ACP
    provider suites.
  - `make test` runs the whole Python and Node suite.
- Verified with Muse 1.4.25 on a host, outside a container, on a real login:
  the ACP provider's model call, composed `CLAUDE.md`, an MCP tool call and a
  session resume, with shell and file-write tools disabled for the check.
  The bearer header, the API host and `SSL_CERT_FILE` trust were probed
  separately.
- Not verified: the harness inside a real agent image, including shell tools
  with `--disable-sandbox`; a real OneCLI gateway swap in a live worker
  container; any production node.
