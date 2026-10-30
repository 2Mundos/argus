# Codex accounts: import, dedicated login, engine selection

Builds on [engines](engines.spec.md) and the account pool in
[worker lifecycle 3.9](worker-lifecycle.spec.md). Single-node rules apply
([spec](single-node.spec.md)): no new service; one small migration (lease roles).

# 1. Purpose and Scope

Make a ChatGPT login (a Codex `auth.json`) a normal Argus **account**, by two
equal paths that end in the same record, and let the pool choose the engine:

- **Import**: the operator gives an `auth.json` they are already authorised to
  use (for example the Codex Desktop file). The dashboard sends the document;
  Argus validates its shape and stores it encrypted (SOPS) as an account.
- **Dedicated login**: Argus drives the official Codex client's device-code
  login for this account only and stores the resulting `auth.json` the same way.
- **Lease and apply**: a worker leases the account like any other; the NanoClaw
  channel gives the credential to the OneCLI vault (secret type `openai`), which
  serves the container a sentinel `auth.json` and swaps the real token on the
  wire. The channel also selects the NanoClaw provider (`codex`/`claude`) that
  matches the leased account.
- **Fallback between engines**, explicit and evented.

# 2. Non-Goals

- Reading `~/.codex`, Hermes or any host login by itself. Import is something
  the operator does, naming the document; Argus never looks in those places.
- Mounting host login directories into workers.
- Inventing an API key for a ChatGPT login, or measuring balance by a paid probe.
- A balance model. Quota is what providers report (`exhausted`, or the window
  percentages a free read-only probe returns, see
  [account usage](account-usage.spec.md)), otherwise unknown, and unknown is
  never treated as zero.
- A second credential store. SOPS is the store; OneCLI is the delivery path.

# 3. Decisions Made

- **Account shape.** `provider: "openai"`, `kind: "subscription"`, credential =
  the `auth.json` document as is (`auth_mode`, `tokens.{id_token,access_token,
  refresh_token,account_id}`, `last_refresh`). `auth_mode: "apikey"` documents
  (`OPENAI_API_KEY`) are accepted as `kind: "api_key"`. Argus validates the
  shape on create and update; errors name the missing field, never a value.
  Responses and events never contain the document.
- **Import and login produce identical records.** Neither path is a condition
  of the other: a Desktop-authorised import activates Codex without any login.
- **Dedicated login** is a session object held in memory (not a table):
  `POST /api/accounts/codex-login` starts it (admin), `GET …/{id}` reads
  status, `DELETE …/{id}` cancels, completion creates the account. Rules:
  - runs the pinned official client's `app-server` over stdio; the pin is
    `ARGUS_CODEX_LOGIN_IMAGE` (the agent image) or `ARGUS_CODEX_BIN`; Argus
    sends `initialize`, then `account/login/start {type: "chatgptDeviceCode"}`
    and waits for the `account/login/completed` notification;
  - a private temporary `CODEX_HOME` (mode 0700, file credential store), deleted
    on completion, cancel, expiry or failure; the client never sees another
    account's files;
  - the response carries `login_id`, `verification_url`, `user_code`,
    `expires_at`. The URL must be `https` on an allow-listed OpenAI host
    (`auth.openai.com`, `chatgpt.com`, `openai.com`); anything else aborts the
    session. The code is a one-time value shown only to the starting admin, not
    logged and not placed in events;
  - at most 2 sessions at a time, 15 minutes each; one session per starting
    token; the process is killed with its group on cancel or expiry;
  - the child's stdout/stderr are never logged; only a fixed status vocabulary
    (`pending`, `completed`, `failed`, `cancelled`, `expired`) and an error
    class are kept.
- **Two leases per agent: engine and executor.** A Codex worker delegates code
  to Claude Code, so it needs two accounts at once. Leases carry a `role`
  (migration `0013`): one active lease per agent and role. The `engine` lease is
  the providers-in-preference lease above; when it lands on `openai`, the
  channel also takes an `executor` lease on `anthropic` (best effort: without
  one, `argus-delegate claude` exits `78 auth_missing`, and Codex still works).
  Exhaustion is reported per role and falls back inside that role first. Each
  held account is usage-accounted separately.
- **Delivery.** The channel maps provider `openai` to OneCLI secret type
  `openai` with the whole `auth.json` text as value (host `chatgpt.com`) or the
  API key (host `api.openai.com`). One OneCLI secret per provider per agent
  group (`argus-<group>` for anthropic, `argus-<group>-openai`); in selective
  mode **exactly the secrets of the leases currently held** are assigned, so a
  released or replaced lease leaves no usable residual secret. No token is written to the
  container, the image, the checkout or a log. Refresh is OneCLI's: Argus does
  not rotate tokens from two places.
- **Engine selection.** The leased account's provider decides the NanoClaw
  provider of the worker's agent group (`openai`→`codex`, `anthropic`→`claude`).
  The channel changes it only between tasks or when a limit forces a switch,
  and then restarts that group's session container so the next turn starts on
  the new harness. Each switch is reported to Argus (`engine.selected` event
  with account id, never credentials).
- **Rotation replaces the runtime.** After a limit or expired-auth report leads
  to a replacement lease (also within one provider, where the harness does not
  change and nothing else would stop the container), the channel stops the
  group's running session containers and waits for each to exit before it resends
  the task, so the next container is built with the stubs (credential, workspace
  id) of the account now held. The task id, attempt, leases and workspace are
  untouched; the rotation costs no attempt. The initial lease stops nothing. If a
  container cannot be stopped (30 s), the task is not resent to the old process:
  the attempt is failed with that reason.
- **Structured auth-expiry signal.** A NanoClaw failure notice
  (`failureNotice: true`) may carry `content.providerFailure =
  { provider: "codex", kind: "auth_expired" }` next to a generic text.
  `codex` names the NanoClaw engine; its Argus account provider is `openai`.
  Only that exact pair, read only inside a `failureNotice` message and only
  while the held engine lease is `openai`, reports the account exhausted
  (`POST /accounts/{id}/exhausted`, role `engine`, default cooldown, no
  invented reset) with the fixed reason `OpenAI/Codex authentication expired`;
  the notice or log text is never copied. It fails closed: unknown provider or
  kind, another account provider, a bad shape, or the field outside a failure
  notice is an ordinary failed attempt. Free text never exhausts an account
  beyond the recognised quota/auth notices above. A Codex auth rejection also
  writes `account.auth_rejected` for the account, in addition to the exhaustion;
  the exhaustion behaviour is unchanged and the new event is written alongside it.
- **Lease order is global priority.** Superseding the earlier Codex-first
  provider order: the pool ranks every usable account across providers by
  priority ([global account selection](account-selection.spec.md)), and an
  exhaustion fallback does the same. The engine follows the leased account's
  provider. A fixed executor (`argus-delegate EXECUTOR`) never changes engine
  silently; the switch happens at the lease, is explicit, and is an event.
- **Catalogue authenticated fact comes from the registry.** For engines with an
  `account_provider`, `authenticated` is `true` when an account exists that is
  not disabled (exhausted ones count: the credential exists); with none, the
  ledger's fact stands (a native login inside a worker) and an unknown becomes
  `false`. The entry carries counts of available, exhausted and disabled
  accounts. `usable` additionally needs one **available** account: only a
  reported `exhausted` or a disabled account removes it; quota that is unknown
  stays available.
- **Routing follows account priority.** The engine router picks the usable
  harness engine (`codex` or `claude`) whose best available account has the
  highest priority (ties: engine id); Codex is no longer primary. A code task
  carries `executor` only when the best executor differs from the engine; an
  unusable one never blocks it. `fallback_from` and the reason name an engine
  that was passed over for lack of an available account (none registered, or
  all exhausted or disabled), and the `engine.routed` event carries it too. Any
  other gap (not installed, not validated, an unknown quota) makes that engine
  unusable; with no usable engine the decision is `blocked`.
- **Selection is reported.** The channel reports each engine switch with
  `POST /api/engines/selected` (agent token): `engine.selected` with the
  engine, provider and account id.

# 4. Open Questions

- Whether OneCLI refreshing a token invalidates the copy in SOPS. Until an
  Argus-side refresh path exists, an account that OneCLI refreshed may need
  re-import or re-login; the account is then reported `exhausted`/auth failed by
  the worker. Using the same login on the Desktop at the same time can also
  invalidate it (a refresh token rotates); prefer the dedicated login for
  accounts that must run unattended.
- Per-engine budgets across providers.

# 5. Constraints

- Admin scope for import and login. Workers only lease.
- The `auth.json` is never returned by any list/read endpoint; a lease returns
  it only to the agent holding it (existing rule).
- No network call inside a write transaction; the login process runs outside
  `Database.write()` and only the final account insert is a write.
- Login sessions and their temporary homes do not survive an API restart.

# 6. Implementation Consequences

- `packages/core`: `codex_auth.py` (shape validation and a redacted summary).
- `apps/api`: migration `0013_lease_roles.sql`; leases, release and exhaustion
  take a `role`; account create validates `openai` credentials;
  `accounts/codex_login.py` and routes under `/api/accounts/codex-login`;
  catalogue `authenticated` from the registry; router fallback with a reason;
  settings `ARGUS_CODEX_LOGIN_IMAGE`, `ARGUS_CODEX_BIN`; MCP `route_task` and
  the REST route share the same policy function.
- `integrations/nanoclaw`: `openai` provider mapping, per-provider OneCLI
  secrets, engine selection on lease, `engine.selected` report.
- Dashboard: the Accounts view gets the Codex wizard: import a file, or
  generate a login (code and link on the page), with the resulting account
  shown by id and name only.
