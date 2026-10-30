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
    exactly those bytes to the client's terminal, pauses briefly, then writes
    Enter. The code is `<code>#<state>` (the `#` is required: the client refuses
    a code without it), 1–2048 characters of `[A-Za-z0-9._~#%+/=-]`, never logged,
    echoed or placed in an event or a validation error. **Value and Enter are two
    writes**: measured on the shipped client (2.1.296), its input treats a long
    chunk as a paste and swallows an Enter that arrives in the same chunk, so a
    real code (about 100+ characters) was never submitted while a 60-character one
    was. A second code is refused with `login_code_in_flight` until the client
    answers the first;
  - the client may refuse a code (`OAuth error … status code 400 Press Enter to
    retry.`) and waits for Enter; that Enter makes it start the flow again with a
    **new sign-in link and a new verifier**, so the refused code's link is dead.
    Argus sends that Enter itself, then publishes the new link (`url`) with the
    refusal, and the operator signs in again with the new link. A bare second code
    is never written while the client waits for Enter: it would be swallowed as the
    retry key. Up to 3 codes are accepted per session while it is `pending` and
    the client is alive (`attempts` / `max_attempts` in the view; the 4th gets
    `login_attempts_exhausted`);
  - the client then prints the token. Only a private parser reads it; it is
    stored as `{"oauth_token": …}` and appears nowhere else. The client's renderer
    only writes the cells that changed since its last frame and jumps over cells
    that already show the right character (measured on 2.1.296: the success screen
    is drawn over the prompt row, so it writes `sk-ant-`, jumps over the `o` of
    "code", then the rest). Stripping escape codes from the stream therefore loses
    characters, and a real login failed with `token_not_found`. The output is
    replayed on a 1000x50 screen (`accounts/terminal.py`, the PTY's size) and the
    token is read off that screen, strictly framed: after the success marker, the
    `Your OAuth token (valid for …):` row, then exactly one non-empty row that is a
    whole `sk-ant-oat…` token, then `Store this token securely.` Until that closing
    row is drawn nothing is taken; anything else between label and closing row (a
    wrapped, cut or second value) fails with `token_not_found` and stores nothing;
  - at most 2 sessions, 15 minutes each, one per starting token; the whole
    process group (and a docker container, if used) dies on cancel, expiry or
    failure, and every concurrent teardown caller waits for the one shielded
    teardown;
  - stdout/stderr and raw terminal bytes are never logged or returned; only the
    fixed states (`pending`, `completed`, `failed`, `cancelled`, `expired`), a
    `phase` (`waiting_for_code`, `finishing`, `renewing_link` while the client restarts after a refusal; no link and no code are accepted then) and fixed reason codes. The client's
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
- **Auth rejection signal.** The Claude rejection signal is the SDK assistant
  message field `assistant.error === "authentication_failed"`. Other values
  (`rate_limit`, `billing_error`, `oauth_org_not_allowed`, `account_on_hold`)
  are not credential rejections and are excluded. The pinned Claude Code is
  2.1.296 (`engines/smoke.sh` expects `2.1.296 (Claude Code)`). The value is in
  that binary's message schema but not in the shipped public typings, and live
  emission for an expired or revoked login in `-p` stream mode is **unverified**:
  no forced-failure run has been done, and one needs a credentialed run on a
  disposable credential. On such a rejection the channel records
  `account.auth_rejected` (see the account-selection spec, §3.6, and the Codex
  spec's structured auth-expiry signal).

# 4. Open Questions

- A `setup-token` token cannot be refreshed; it lasts what Anthropic grants. An
  expired one is renewed with this wizard.
- Whether the client's prompts change between versions. The parser keys on the
  URL, the success screen's three fixed rows and the `sk-ant-oat` prefix; a
  changed layout is a fixed reason (`token_not_found`, `no_url`,
  `client_exited`), never a guessed token. Tests replay bytes captured from the
  real 2.1.296 client (against a local stand-in for the token endpoint, with a
  fake token); re-capture them when the pinned client changes.

# 5. Constraints

- Admin scope for import, login and renewal. Workers only lease.
- No credential, code, URL query or client output in a response, listing, event,
  log or error.
- Login sessions and their private directories do not survive an API restart.

# 6. Implementation Consequences

- `packages/core`: `claude_auth.py` (shape validation, derived kind, redacted
  summary).
- `apps/api`: `accounts/login_base.py` (shared), `accounts/claude_login.py`, `accounts/terminal.py` (screen replay),
  routes under `/api/accounts/claude-login`, anthropic validation on create and
  credential update, settings `ARGUS_CLAUDE_LOGIN_IMAGE` / `ARGUS_CLAUDE_BIN`,
  422 bodies no longer echo request input.
- Dashboard: **Add Claude account** (three paths: import file/paste, token with a
  type, dedicated login) and a **Renew** action on each `anthropic` row, which
  opens the same wizard prefilled with that account.
