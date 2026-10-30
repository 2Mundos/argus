# Copilot accounts: import, executor lease, delegated CLI

Builds on [engines](engines.spec.md), the account pool in
[worker lifecycle 3.9](worker-lifecycle.spec.md) and the executor pattern of
[Cursor accounts](cursor-accounts.spec.md). Single-node rules apply
([spec](single-node.spec.md)): no new service; reuse the existing `executor`
lease role.

# 1. Purpose and Scope

Make a GitHub Copilot credential a normal Argus **account**, validate its shape
on create and replace, and deliver it to the **delegated** GitHub Copilot CLI
(`argus-delegate copilot`) without making Copilot an engine harness:

- **Import**: the operator stores a token they are already authorised to use:
  `{"token": "<token>"}`. Accepted token types follow the
  [official CLI authentication rules](https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli):
  a fine-grained personal access token (`github_pat_…`) owned by a personal
  account with the **Copilot Requests** permission, an OAuth token from
  `copilot login` (`gho_…`), or a GitHub App user-to-server token (`ghu_…`).
  Classic tokens (`ghp_…`) are rejected on import: the CLI silently ignores them,
  which would look like "no credential". Errors name fields and the rule broken,
  never a value.
- **Lease**: the `executor` lease role selects a `copilot` account next to the
  engine's account (for example Codex or Claude). Copilot is never an `engine`
  lease and never replaces the primary harness.
- **Delivery**: the NanoClaw channel (trusted host) writes the token into the
  OneCLI vault. The worker holds a **non-secret sentinel** only, and the gateway
  injects the real bearer token on the wire for the hosts the CLI authenticates
  against (§3, delivery). The real token never enters the container, image,
  checkout or a log.
- **Executor selection**: the executor lease is the best usable account across
  all runtime-supported providers by priority
  ([global account selection](account-selection.spec.md)); a Copilot account is
  leased as the executor when it ranks first.

# 2. Non-Goals

- Reading host `~/.copilot`, the OS keychain, `gh auth token`, live Argus
  secrets or `.env` from agents. Import is something the operator does, naming
  the token; Argus never looks in those places.
- A dedicated device-code login driven by Argus (the Codex and Claude pattern).
  It is a possible follow-up; v1 is import only.
- GitHub Enterprise Cloud with data residency (`*.ghe.com`) and GitHub
  Enterprise Server: `github.com` only, as in
  [GitHub repositories](github-repositories.spec.md).
- Making Copilot an engine harness; NanoClaw has no Copilot provider.
- Reading Copilot quota or billing from an undocumented endpoint (for example
  `/copilot_internal/user`): it is not a supported API and may change without
  notice.
- BYOK providers for the CLI (`COPILOT_PROVIDER_*`): those are the
  [custom accounts](custom-accounts.spec.md) concern, not a Copilot account.
- Running a paid Copilot call in CI; fleet acceptance is a parent-run bounded
  smoke with a real leased account.

# 3. Decisions Made

- **Account shape** (`packages/core/src/argus_core/copilot_auth.py`):
  `provider: "copilot"`, `kind: "subscription"`, credential `{"token": …}`.
  Validation on REST create/replace and in the channel before OneCLI apply
  checks the prefix (`github_pat_`, `gho_`, `ghu_`), rejects `ghp_` and any
  other shape, and a document that carries extra keys. Argus cannot check
  offline that a fine-grained token has the Copilot Requests permission or
  that its owner holds a seat; that surfaces as an auth failure at first use
  (below), not as a validation guess. Responses and events never echo the token.
- **Executor-only provider**: `copilot` joins `cursor` in `EXECUTOR_ONLY`; the
  `engine` lease role never returns it, the `executor` role may.
- **Delivery** (`integrations/nanoclaw/argus-accounts.ts`): generic OneCLI
  secrets per agent group (`argus-<group>-copilot-…`), one per host and path
  family, same token value. There is no broad host wildcard: every host and
  path family the pinned CLI needs for authentication and model calls is listed
  explicitly, found by a probe of the pinned CLI version (§4), and other hosts
  and paths are forwarded untouched. `api.github.com` is shared with the
  [repository](github-repositories.spec.md) secrets, so Copilot secrets use
  their own path patterns and never widen a repository secret.
- **Child runtime** (`argus-delegate`): the CLI reads credentials in this
  order: `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, the keychain,
  then `gh`. `argus-delegate copilot` therefore sets `COPILOT_GITHUB_TOKEN` to
  a **non-secret sentinel** shaped like a fine-grained token, so no other
  variable, keychain entry or `gh` login in the container can supply a
  credential, and a missing sentinel is `78 auth_missing`, not a silent
  fallback. `COPILOT_HOME` points at a **per-run writable tree** under
  `{authorized cwd}/.argus-delegate-runtime/copilot-home/` so the CLI never
  writes a plaintext token fallback (`config.json`) into a shared or read-only
  location, and nothing persists across tasks.
- **Invocation**: `copilot -p <prompt> --model <explicit> -s --no-ask-user`;
  the model is always explicit, never the CLI default. Read-only delegation
  grants only read tools; write mode adds `--allow-tool` for the narrow tools
  the task needs. `--allow-all-tools` is not used, `--allow-url` is not used
  unless a task names the URL, and `--share-gist` is never used (it publishes
  the transcript outward). `--share=<path>` is not used either: transcripts
  may contain task data and are not an Argus artifact.
- **Failure classes**: the delegate maps the CLI's failure to the existing
  vocabulary and fails closed. An authentication rejection (expired token,
  missing permission, no seat, organisation policy disabling the CLI) reports
  the account exhausted with a fixed reason and writes `account.auth_rejected`
  ([account usage](account-usage.spec.md) auth-health rules unchanged); a premium-request
  limit reports it exhausted until the reset the CLI states, else the default
  cooldown with no invented reset. Anything not exactly recognised is an
  ordinary failed attempt; free text never exhausts an account.
- **Quota** ([account usage](account-usage.spec.md) 3.3): there is no
  documented per-user API, so a Copilot account reads `unknown` with reason
  `no_provider_source`, never 0. Usage is accounted from the delegate report
  when the CLI states it; otherwise it is recorded without a figure.
- **Engine catalogue** (public API): Copilot is `role: executor` only.
  Readiness for a running worker is the sentinel present plus gateway injection
  configured on the host, as for Cursor; `usable` needs one **available**
  account, and `authenticated` comes from the registry
  (the catalogue rule in [Codex accounts](codex-accounts.spec.md)).

# 4. Open Questions

- Exact hosts and path families the pinned CLI uses for model calls and token
  validation, and whether it checks the token prefix client-side (the sentinel
  must pass it). Resolve with a probe of the pinned version before the
  implementation PR; the official page lists no endpoints.
- The CLI's exit codes and error text for auth rejection and premium-request
  limits, and whether it has a stable JSON output mode for the delegate to
  parse. Until verified the delegate treats every non-zero exit as an ordinary
  failure.
- Whether OneCLI can hold a `gho_` OAuth token the CLI would otherwise refresh,
  and whether a dedicated device-code login (`copilot login --device-code`) is
  worth adding later.
- GitHub's terms for running a personal Copilot seat unattended in a shared
  fleet, and whether the June 2026 change in Copilot billing alters quota
  semantics. The operator owns that decision; Argus does not enforce it.

# 5. Constraints

- Admin scope for import and credential replace. Workers lease only; the
  container never sees the real token.
- Lease apply runs on the NanoClaw host, not in the worker container.
- No credential value in a response, listing, event, log, error, or delegate
  report.
- No network call inside a write transaction.

# 6. Implementation Consequences

- `packages/core`: `copilot_auth.py`; `accounts.py` role eligibility
  (`EXECUTOR_ONLY`).
- `apps/api`: copilot validation on create/replace; tests in
  `test_copilot_accounts.py`; usage reason for a source-less provider.
- `integrations/nanoclaw`: `argus-accounts.ts` (mapping and apply),
  `argus.ts` (executor lease), `argus-delegate` (`copilot` subcommand,
  sentinel, private `COPILOT_HOME`), `install.sh` hooks into `onecli.ts`, and
  the agent image gains the pinned `@github/copilot` CLI.
- Dashboard: the Accounts view lists `copilot` with a provider mark and an
  import form that states the accepted token types.
- **Acceptance (automated)**: `make check`; channel/gateway tests after
  `install.sh` into a NanoClaw checkout; fake-credential delegate/proxy probes
  (no live Copilot call in CI).
- **Acceptance (fleet)**: parent-run `argus-delegate copilot` read-only with an
  explicit model, native CLI exit code 0, and an exact controlled nonce echoed
  in the CLI result. Record the outcome in a dated ref under `docs/runbooks/`
  when verified.
