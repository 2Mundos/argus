# Login clients

# 1. Purpose and Scope

The dedicated Claude and Codex logins ([claude accounts](claude-accounts.spec.md),
[codex accounts](codex-accounts.spec.md)) and the Codex quota probe
([account usage](account-usage.spec.md)) run the pinned official client. A
container install (the [launcher](launcher.spec.md), `docker run`, Compose) had
neither client, and `ARGUS_*_LOGIN_IMAGE` cannot work from inside a container
(no `docker` CLI, and the session directory is a path the host daemon cannot
mount). "Sign in with Claude" therefore failed on every packaged install.

Argus now downloads the pinned client the first time it is needed and keeps it
in the data volume. The image stays the same size; installs that set a client
explicitly behave as before.

# 2. Non-Goals

- No bundling in the image. Both clients add about 550 MB (Claude 2.1.296:
  257 MB, Codex 0.162.1: 294 MB), which would roughly double the image for a
  step most installs run a few times.
- No auto-update and no "latest". A client changes only when a release changes
  its pin, after it has been measured against that client (claude accounts 3).
- No download for anything but a login the operator starts. Background loops
  (the quota probe) use a client already present and never fetch one.
- No macOS or Windows binaries: host-run installs there set `ARGUS_CLAUDE_BIN`
  / `ARGUS_CODEX_BIN` as before.

# 3. Decisions Made

## 3.1 Which client a login runs

In order: `ARGUS_*_LOGIN_IMAGE`, then `ARGUS_*_BIN`, then the downloaded client.
Downloads are on unless `ARGUS_CLIENT_DOWNLOADS=0`, and apply on Linux
`x86_64` and `aarch64` only. A login is offered (`configured`) when one of the
three applies.

## 3.2 The pins

`argus_api/accounts/clients.py` holds one entry per client and platform: the
version, the URL and its SHA-256.

| Client | Version | Source |
| --- | --- | --- |
| `claude` | 2.1.296 | Anthropic's release bucket (`claude-code-releases/<version>/<platform>/claude`; the release manifest lists the checksums), `linux-x64` / `linux-arm64`, the `-musl` variants when the C library is not glibc |
| `codex` | 0.162.1 | GitHub release `rust-v0.162.1`, `codex-<arch>-unknown-linux-musl.tar.gz` (static), one member extracted |

The versions match the agent image ([engines](engines.spec.md)): Claude Code
2.1.296 is the client the login was measured on, Codex 0.162.1 the
`install-engines.sh` pin. Changing a pin is a code change with new checksums.

## 3.3 Download and install

On the first `POST /api/accounts/{claude,codex}-login` that needs it, before the
session starts:

- one download per client at a time (a lock per client; a second request waits
  for the first and then uses its result);
- streamed into a temporary file under `<data_dir>/tools/`, hashed while it is
  written, and refused when the size passes 600 MB or the SHA-256 differs;
- the binary is made executable and renamed to
  `<data_dir>/tools/<client>-<version>/<client>`, so a present file is always a
  verified one; directories of other versions of that client are then removed.

It is a network call outside any write transaction. A failure (no network,
checksum mismatch, unsupported platform) fails the start with
`*_login_unavailable` and a message that names the cause and the token or
import path; nothing partial is kept. The first start waits for the download
(tens of seconds); later starts do not download.

## 3.4 The quota probe

The Codex probe uses `ARGUS_CODEX_BIN`, else the downloaded client if it is
present, else it skips with `probe_disabled` as before. A Codex sign-in
therefore also turns on the probe.

## 3.5 Dashboard

While a start is in flight the login dialog says the client may be downloading
on first use. When the API answers `*_login_unavailable`, the dialog shows its
message and points to the other paths: for Claude, run `claude setup-token` on
your own computer and paste the token under "Use a token or API key"; for
Codex, import `~/.codex/auth.json`.

# 4. Open Questions

1. Whether to fetch the client when the account dialog opens, so the first
   start does not wait. Not done: it downloads for operators who only import.

# 5. Constraints

- Standard library only (`urllib`, `hashlib`, `tarfile`), run in a thread.
- The data volume holds at most one version per client (≤ 550 MB in all).
- `ARGUS_CLIENT_DOWNLOADS=0` makes Argus make no request to the release hosts.

# 6. Implementation Consequences

- `apps/api/src/argus_api/accounts/clients.py`; `Settings.client_downloads`.
- `claude_login.py` / `codex_login.py`: resolve the client per 3.1 before the
  session starts; `quota_probe.py`: 3.4.
- Dashboard: the Claude and Codex login dialogs (3.5).
- [Claude accounts](claude-accounts.spec.md), [codex accounts](codex-accounts.spec.md)
  and [account usage](account-usage.spec.md) name the downloaded client next to
  the two settings.

## Verification boundary

- Commands: `uv run pytest apps/api/tests/test_clients.py` and the login tests;
  `make check`.
- Verified: download, checksum refusal, size cap, extraction, one download for
  concurrent starts, cleanup of other versions, client resolution order and the
  probe's no-download rule, against a fake fetch (unit level). The pinned
  checksums match the publishers' manifest and release digests on 2026-11-03.
- Not verified: a real sign-in with a downloaded client inside the image, on
  `x86_64` and `aarch64`; to be done on the first release that ships this.
