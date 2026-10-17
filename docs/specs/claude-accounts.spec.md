# Claude accounts: import, dedicated login, renewal

Builds on [Codex accounts](codex-accounts.spec.md) (same account, vault and
login-session contracts) and the account pool in
[worker lifecycle 3.9](worker-lifecycle.spec.md). Single-node rules apply
([spec](single-node.spec.md)): no new service, no migration.

# 1. Purpose and Scope

Make an Anthropic credential a normal Argus **account** by two equal paths, and
let the operator **renew** an existing one without recreating it:

- **Import**: the operator gives a credential they are already authorised to use,
  by file or paste (Claude's native credentials JSON), or as a single token with
  an explicit type (OAuth token or API key).
- **Dedicated login**: Argus drives the official Claude client's
  `claude setup-token` for this account only (a Claude subscription's long-lived
  token) and stores the result the same way.
- **Renew**: either path can target an existing `anthropic` account. Its name,
  priority, `max_leases`, labels and enabled/disabled state stay as they are;
  only the credential (and the derived kind) changes.

# 2. Non-Goals

- Reading `~/.claude`, the desktop CLI login, Hermes or any host login. Import is
  something the operator does, naming the document.
- Console (billing) logins, SSO or `claude auth login`: the client can persist
  Console state outside its config directory, and its subscription login is
  short-lived. `setup-token` is the unattended path.
- Reimplementing Anthropic's OAuth over HTTP, a browser driver, or asking for a
  password. The official client does the exchange; Argus relays the one value it
  asks for.
- Model tools that collect secrets: credentials never travel through MCP or chat.

# 3. Decisions Made

- **Credential shapes** (`provider: "anthropic"`). Exactly one of:
  `{"api_key": …}` (kind `api_key`), `{"oauth_token": …}` (kind `subscription`;
  the `setup-token` output), or Claude's credentials file
  `{"claudeAiOauth": {"accessToken", "refreshToken", "expiresAt", …}}` (kind
  `subscription`). Anything else, including a bare `token`, is rejected: an
  unknown type is not shown as authenticated. A `claudeAiOauth` whose `expiresAt`
  is past is rejected (its remedy is a fresh login). Token prefixes must match
  the declared type (`sk-ant-oat…` is never an `api_key`). Errors name fields
  (`claudeAiOauth is missing: refreshToken`), never values.
- **Kind is derived** from the credential on create, renew and credential update;
  the form does not ask for it.
- **Dedicated login** is a session object in memory, like the Codex one:
  `POST /api/accounts/claude-login` (admin; optional `name`, or `renew_account_id`),
  `GET …/{id}`, `POST …/{id}/code`, `DELETE …/{id}`. Rules:
  - runs the pinned client (`ARGUS_CLAUDE_LOGIN_IMAGE` = the agent image, or
    `ARGUS_CLAUDE_BIN`) as `claude setup-token` on a **pseudo-terminal** (standard
    library `pty`); the terminal is 1000 columns wide so nothing wraps;
  - `HOME`, `CLAUDE_CONFIG_DIR`, `XDG_*`, `TMPDIR` and the working directory are
    one private 0700 directory under the Argus data dir, deleted on completion,
    cancel, expiry or failure. The environment is an allowlist: no
    `ANTHROPIC_*`, `CLAUDE_CODE_OAUTH_TOKEN` or Hermes variable is inherited.
    `BROWSER` and `xdg-open` are stubs so no desktop browser opens;
  - the first URL the client prints is the sign-in link. It must be `https` on
    `claude.ai`, `claude.com`, `anthropic.com` or a subdomain, with no
    credentials in it; anything else aborts the session;
  - the operator signs in with the link; the page shows a code; the operator
    pastes it **into the Argus page** (never into chat), and `POST …/code` writes
    exactly those bytes plus Enter to the client's terminal. The code is
    1–2048 characters of `[A-Za-z0-9._~#%+/=-]`, never logged, echoed or placed
    in an event or a validation error. It is written with its Enter in a single
    write, like a paste. The client may refuse a code and keep waiting, so up to 3
    codes are accepted per session while it is `pending` and the client is alive
    (`attempts` / `max_attempts` in the view; the 4th gets
    `login_attempts_exhausted`);
  - the client then prints the token. Only a private parser reads it; it is
    stored as `{"oauth_token": …}` and appears nowhere else;
  - at most 2 sessions, 15 minutes each, one per starting token; the whole
    process group (and a docker container, if used) dies on cancel, expiry or
    failure, and every concurrent teardown caller waits for the one shielded
    teardown;
  - stdout/stderr and raw terminal bytes are never logged or returned; only the
    fixed states (`pending`, `completed`, `failed`, `cancelled`, `expired`), a
    `phase` (`waiting_for_code`, `finishing`) and fixed reason codes. The client's
    output is classified by fixed markers into a closed code: `oauth_<status>`
    (its "OAuth error … status code NNN"; only the number), `code_invalid`,
    `token_not_found` (it reported success but no token was recognised; nothing is
    stored), `login_rejected` (it left after a code with no known marker),
    `client_exited`, `no_url`. While a refusal leaves the login `pending`, it is
    shown as `refusal`.
- **Shared with Codex**: sessions, ownership, TTL, limits, teardown and the
  commit that cancel/expiry cannot interrupt live in one base
  (`accounts/login_base.py`); Codex behaviour is unchanged.
- **Renewal is atomic.** For a login: the new secret replaces the old one, then the
  kind/event row is written in `Database.write()`; if that fails the previous
  credential is restored. Import renewal is `PUT /api/accounts/{id}/credential`,
  now validating `anthropic` documents and syncing the kind. No network call
  happens inside a write.
- **Docker mode** (image) runs `--cap-drop=ALL`, `no-new-privileges`, the caller's
  UID, a memory/pids limit and no Docker socket.

# 4. Open Questions

- A `setup-token` token cannot be refreshed; it lasts what Anthropic grants. An
  expired one is renewed with this wizard.
- Whether the client's prompts change between versions. The parser keys on the
  URL and the `sk-ant-oat` token only, and a failed parse is a fixed reason
  (`no_url`, `client_exited`), not a stall.

# 5. Constraints

- Admin scope for import, login and renewal. Workers only lease.
- No credential, code, URL query or client output in a response, listing, event,
  log or error.
- Login sessions and their private directories do not survive an API restart.

# 6. Implementation Consequences

- `packages/core`: `claude_auth.py` (shape validation, derived kind, redacted
  summary).
- `apps/api`: `accounts/login_base.py` (shared), `accounts/claude_login.py`,
  routes under `/api/accounts/claude-login`, anthropic validation on create and
  credential update, settings `ARGUS_CLAUDE_LOGIN_IMAGE` / `ARGUS_CLAUDE_BIN`,
  422 bodies no longer echo request input.
- Dashboard: **Add Claude account** (three paths: import file/paste, token with a
  type, dedicated login) and a **Renew** action on each `anthropic` row, which
  opens the same wizard prefilled with that account.
