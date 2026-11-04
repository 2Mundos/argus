# Desktop Tray

# 1. Purpose and Scope

`argus up` automatically starts a desktop companion to the
[launcher](launcher.spec.md) after the server becomes ready. Its menu opens the
configured dashboard in the default browser, starts, stops and restarts the
installation, creates a backup, shows recent logs, and displays service health
and basic statistics.

# 2. Non-Goals

- No separate desktop dashboard, database access or worker implementation.
- No automatic start of Argus, upgrades, uninstall, or changes to desktop login
  settings. Starting the tray alone leaves the server's current state intact.
- No native Windows server or duplicate Windows launcher. On Windows, Argus and
  Docker access stay in WSL 2; the tray and browser run on Windows.

# 3. Decisions Made

## 3.1 Optional host application

Linux, macOS and Windows use Qt's native system tray through `PySide6-Essentials`.
`uv` is required only for the tray; it installs the script's pinned Python
dependency in an isolated cache. The regular launcher still needs only Docker
and curl. Linux requires a desktop with a system tray; GNOME may need its
AppIndicator extension. A missing display or tray produces an actionable error.

On Linux with a desktop display, macOS outside SSH, and Windows through WSL 2,
`up` starts the tray in the background once Argus is ready. `up --no-tray` skips
launching it for that invocation; an existing tray stays open. Headless sessions
skip it. If `uv` is missing, `up` explains how to install it and still succeeds.
Tray startup errors never fail the server start and are written to
`$ARGUS_HOME/tray.log` (mode `0600`). `argus tray` remains a foreground command
for manual startup and diagnostics. Tray Up and Restart use `up --no-tray`.

The image carries `/app/bin/argus-tray.py`. The launcher extracts the
script matching the configured version into `$ARGUS_HOME/tray/`, then executes
it using `uv run --script`. Nothing is installed into the API's Python environment.
The first installation is completed with `argus up` in a terminal before its
automatic tray launch. Only one tray per config directory runs at a time, so
repeated `up` calls cannot add duplicate icons. The tray survives terminal closure
and stays available after `argus down`; Quit Tray leaves the server running.
After confirmation, `argus uninstall` stops the tray before removing any install
resources, including with `--keep-data`. It uses `argus tray --stop` to request
shutdown over a local Qt socket restricted to the same user, and waits for an
acknowledgement after the icon is hidden, refresh work has finished, and its
singleton lock is released. Windows uses the existing WSL-to-Windows bridge for
this command. Shutdown needs no display and never signals a PID. If the tray is
busy or cannot be contacted, uninstall fails before deleting resources and asks
the operator to quit the tray and retry. A stale lock with no live tray is safe
to clear through Qt's existing lock ownership checks.

On Windows, install and run Argus in a WSL 2 distribution with Docker available,
and install `uv` on Windows. Running `argus up` inside WSL detects the
distribution and invokes `uv.exe` with the script's Windows path. The native
Windows process calls `wsl.exe --distribution ... --user ... --exec` with argument
arrays to run launcher and Docker commands in the original distribution as the
original user. It passes the original WSL config directory explicitly. No shell
command strings, Windows Docker CLI or Linux desktop tray are required. Windows
accesses the API through WSL's localhost forwarding; custom WSL networking must
preserve that route. The automatic tray launch detaches the native Windows process
from the original WSL terminal.

## 3.2 Controls reuse the launcher

Up, Down, Restart (the launcher's `up` reapplies the configuration), Backup and
Logs invoke the same installed launcher with the same `ARGUS_HOME`. They inherit
its locking and named-container boundaries. Commands and periodic status reads
run off the GUI thread. While a command runs, other commands and Quit are disabled;
failures remain visible in a dialog. Quitting the tray leaves Argus running.
Recent logs remain available for an existing stopped or crashed container;
stopping the container does not erase its logs.

Open Argus uses `http://127.0.0.1:<configured port>/` without credentials in the
URL. Double-clicking the icon also opens it. The tray reads version, port and
container through the launcher's internal `__tray-config` command, so it never
parses or executes the config independently. Settings are refreshed every ten
seconds and on request.

## 3.3 Status and statistics use existing API contracts

Container inspection distinguishes a stopped or missing installation from Docker
being unavailable. `/readyz` determines Ready versus Unhealthy; a failed request
is Unreachable. Disabled menu rows display version, task counts (claimed, ready,
blocked), active and draining agents, open decisions, and uptime from
`GET /api/system`. All engines, including Claude, Codex, Cursor, Copilot, custom
and local, are represented by that existing service's totals.

The generated admin token is obtained through `argus token` and used only in an
HTTP Authorization header to the configured loopback server. Proxy handling and
redirects are disabled. A preset or rotated token can be entered with Sign in
for stats; it stays in memory for the tray session. Missing or rejected credentials
leave service controls available and statistics explicitly unavailable. Failed
token lookups, including timeouts, preserve the service health and dashboard URL.
Failed refreshes clear old counts. Tokens never enter browser URLs, subprocess arguments,
dialogs, logs or persistent tray files.

# 4. Open Questions

- Desktop login integration and packaged applications may be added separately
  after native tray behavior has been checked on Linux and macOS.

# 5. Constraints

- No API, MCP, schema, engine or database changes: this is a host launcher surface
  using existing commands and the existing system-summary API model.
- Status reads have bounded network and subprocess timeouts. Only one refresh and
  one launcher command may be in flight; the GUI event loop remains responsive.
- The tray uses the existing Argus brand geometry and a service-status badge.
- No real model runs or changes to the live preview are required for verification.

# 6. Implementation Consequences

- `deploy/argus-tray.py` is an optional Python script with inline dependency metadata.
- `deploy/argus` starts the tray from `up`, with a `--no-tray` opt-out, and provides
  `tray` (including its Windows/WSL bridge) and the private
  `__tray-config` command;
  `deploy/Dockerfile.api` packages the companion beside the launcher.
- The README documents invocation, prerequisites and menu behavior; the launcher
  spec describes this optional exception to its host dependency requirements.

## Verification boundary

- Commands: `uv run pytest scripts/test_launcher.py`; `uv run ruff check
  deploy/argus-tray.py`; `uv run ruff format --check deploy/argus-tray.py`;
  `python3 scripts/validate_docs.py`; POSIX shell syntax checks.
- Verified: existing launcher behavior against its Docker and curl fakes; tray
  syntax, formatting and headless smoke checks with disposable launcher/API data,
  including automatic startup, headless/opt-out paths, background detachment,
  optional token lookup failures, stopped-container logs, and uninstall shutdown.
- Not verified: real menu placement, browser launching and notifications in a
  Linux, macOS or Windows desktop session; the WSL-to-Windows process bridge and
  localhost forwarding on a real Windows machine; extraction from a published
  release awaits the image containing the companion.
