---
status: implemented
created: 2026-11-03
updated: 2026-11-06
---

# Local workers

# 1. Purpose and Scope

A [launcher](launcher.spec.md) install runs Argus in two commands, but a worker
on the same machine still takes a host-level setup: a NanoClaw checkout built
with `integrations/nanoclaw/provision.sh` (Node, pnpm, git on the host), a
~6 GB agent image built locally, the OneCLI gateway installed by hand
([runbook](../runbooks/2026-10-16.nanoclaw-worker-setup.ref.md)), about ten
`ARGUS_NANOCLAW_*` variables, and bind mounts the launcher cannot express.
Until then the "New worker" dialog says `ARGUS_PUBLIC_URL is not set` and no
template is ready.

Every install needs workers to run a task, so this spec makes them part of
the first `argus up`, which asks before it gives Docker access or pulls the
images (3.7). The steps it runs are also commands of their own:

```bash
argus worker setup     # pull the worker images, start the gateway, reconfigure Argus
argus worker status    # what is set up, versions, gateway health
argus worker remove    # undo it (keeps gateway data unless --purge)
```

After `setup`, the dashboard's "New worker" offers a ready
`nanoclaw-socket` template, and tasks run with the Argus accounts the operator
added. Every release publishes the worker images, and `argus update` keeps them
at the same version as Argus.

# 2. Non-Goals

- No change to Argus's own dependencies. Argus stays one process and one
  SQLite file ([single-node](single-node.spec.md)); the gateway and its Postgres
  are NanoClaw's dependency, started only after the operator says yes (3.7).
  Argus starts and serves the dashboard without them, as before.
- No `nanoclaw-sandbox` through the launcher. It needs `sysbox-runc`, a
  host-level install; the manual route ([worker launch](worker-launch.spec.md),
  the runbook) stays for it.
- No remote Docker hosts or Kubernetes: `worker setup` prepares this machine.
- No change to how the fleet starts, leases or isolates workers. The launcher
  produces the same settings `provision.sh` documents.
- No arm64 worker images (the API image is multi-arch) ([releases](releases.spec.md), open question 1).

# 3. Decisions Made

## 3.1 Two images per release

The release workflow also publishes, from the pinned NanoClaw commit
(`provision.sh`'s `NANOCLAW_COMMIT`) with the engines overlay applied, so every
engine the fleet supports is present:

- `ghcr.io/2mundos/argus-nanoclaw-host:vX.Y.Z`: `deploy/nanoclaw-host` plus the
  **built** checkout at `/opt/nanoclaw` (Argus channel, gateway adapter, engines
  overlay, `dist/`). Its `argus.nanoclaw.commit` label names the commit.
- `ghcr.io/2mundos/argus-nanoclaw-agent:vX.Y.Z`: NanoClaw's agent image with the
  Argus tools layer, what `provision.sh` tags `argus-nanoclaw-agent:<commit>-engines`.

`deploy/nanoclaw-host/build-release.sh` builds both with the scripts
`provision.sh` runs (no second recipe): `Dockerfile.release` runs
`provision.sh check` on the runtime image's Node (so native modules match) and
copies the checkout in; the agent image is NanoClaw's `container/build.sh` on a
copy of that checkout plus the Argus tools layer. Quality CI builds the host
image (which runs the Argus vitest suite) when `integrations/nanoclaw/` or
`deploy/nanoclaw-host/` change; the agent image is built by the release
workflow only. Both carry the OCI source, version and revision labels, and
`latest` follows the newest release as for `argus` (releases 3.7).

## 3.2 The checkout lives on the host

NanoClaw hands bind-mount paths to the Docker daemon, which resolves them on
the host, so the checkout and the instance directories must be host paths seen
at the same path by every container (integrations/nanoclaw README). `setup`
copies `/opt/nanoclaw` out of the host image (`docker create` + `docker cp`) to
`$ARGUS_DATA_HOME/nanoclaw/<version>/src` and creates the instances root
`$ARGUS_DATA_HOME/nanoclaw/i`, where `ARGUS_DATA_HOME` defaults to
`${XDG_DATA_HOME:-~/.local/share}/argus`. The instances root must stay at most
56 characters (provision.sh); `setup` refuses a longer one and names
`ARGUS_DATA_HOME` to shorten it. Files are owned by the host user, which is
also `ARGUS_NANOCLAW_USER`. Checkouts of other versions are removed once the
new one is in use.

## 3.3 The gateway

`setup` runs OneCLI as NanoClaw expects it, following the runbook's
single-node layout:

- `ghcr.io/onecli/onecli` **1.42.0** and `postgres:18-alpine`, both pinned by
  digest in the launcher (1.43+ drops the secret-assignment API the channel
  uses). Containers `argus-onecli` and `argus-onecli-db`, volumes
  `argus-onecli-data` and `argus-onecli-pg`, `restart: unless-stopped`.
- The management API (10254) is published on `127.0.0.1` only and joins a
  private network `argus-onecli-control` with the NanoClaw **host**
  containers only, under the network alias `onecli` (the only private name Argus
  accepts, `http://onecli:10254`); Postgres publishes no port; the egress proxy (10255) is
  published on loopback and the Docker bridge address so agent containers reach
  it as `host.docker.internal:10255`.
- Generated secrets (Postgres password, OneCLI keys) go in
  `$ARGUS_HOME/onecli.env`, mode 0600; they are passed with `--env-file` and
  never printed.
- No gateway credential step: account leasing is on when the pool has a OneCLI
  URL, and the host writes the leased Argus account to the vault
  ([account selection](account-selection.spec.md)).

## 3.4 What `setup` changes in Argus

It sets `ARGUS_DOCKER=1` and `ARGUS_WORKERS=1` in the config and restarts Argus.
With `ARGUS_WORKERS=1`, every start of the Argus container derives the worker
settings from the config and the running version, so they can never point at
another version's images or checkout: the settings `provision.sh env` prints
for the socket template (`ARGUS_NANOCLAW_INSTALL_DIR` =
`<data home>/nanoclaw/<version>/src`, the instances root, the two published
image refs of that version, the opt-in, user and socket group),
`ARGUS_NANOCLAW_ONECLI_URL=http://onecli:10254` and
`ARGUS_NANOCLAW_NETWORK=argus-onecli-control`, plus a bind mount of
`<data home>/nanoclaw` at its own path. None of them is secret. `setup` asks
for confirmation first, because the socket template and Docker access are
root-equivalent on this Docker host (`-y` skips it). `up --no-docker` is
refused while workers are set up, and every `up` with workers set up also
starts the gateway if it is missing (after an `uninstall --keep-data`, say).

## 3.5 Updates and the way back

- `argus update` and `argus restore` with workers set up pull the worker images
  of the version they move to and copy its checkout **before** stopping Argus;
  a release without worker images stops the command there, with nothing
  changed. After a successful update, other versions' checkouts are removed; a
  rollback keeps the previous one, and starting the previous version points at
  it again. Running workers are replaced by the fleet's own drift handling when
  Argus restarts with the new images.
- `argus worker remove` stops and removes the gateway containers and network,
  sets `ARGUS_WORKERS=0` and restarts Argus without the worker settings. Pools
  that use the socket template become not-ready and say why, as for any missing
  prerequisite. `--purge` also deletes the gateway volumes, the checkouts and
  `onecli.env`; without it they stay for a later `setup`. Argus's own volume is
  never touched.
- `argus worker status` shows the worker images, the checkout and the gateway
  containers' state; the dashboard's "New worker" shows the template's
  readiness.
- `argus uninstall` removes everything `setup` created, as `remove --purge`
  does plus the images ([launcher](launcher.spec.md) 3.9).

## 3.6 Messages point to the launcher

The launcher sets `ARGUS_LAUNCHER=1` on the Argus container. Readiness reasons
that name a variable the launcher manages (`ARGUS_PUBLIC_URL`, the NanoClaw
settings) then say which command fixes them: `argus up --docker` or
`argus worker setup`. Other installs keep today's messages.

## 3.7 The first `up` offers workers

On the first run ([launcher](launcher.spec.md) 3.8), `up` asks, before it
pulls or starts anything:

```
Set up workers on this machine? They run coding agents in Docker.
  - Argus gets the Docker socket: root-equivalent on this host.
  - Pulls about 7 GB of images and starts the OneCLI gateway and its Postgres.
Set up workers? [Y/n]
```

- Yes runs the `setup` steps (3.2 to 3.4) and then starts Argus once, with the
  worker settings. No sets `ARGUS_WORKERS=0` and starts Argus as `up` did
  before this spec.
- `--workers` answers yes and `--no-workers` answers no without asking.
  Without a terminal and without either flag, `up` answers no and prints
  `argus worker setup`, so scripted installs never gain Docker access
  silently.
- If a worker step fails (a pull, the disk, the gateway), `up` still starts
  Argus without workers, prints the error and `argus worker setup` to retry,
  and exits non-zero. Setup's own checks (3.2's path length) run before the
  question, so a machine that cannot run workers is told so instead of asked.
- `up --workers` (or `--no-workers`) on a later run is refused and names
  `argus worker setup` (or `remove`): the first run is the only time `up`
  decides it.

# 4. Open Questions

1. Registry cost: the agent image is about 6 GB per release. Keep only the last
   N releases' agent images in GHCR, and with which N?
2. Build time and disk on the GitHub runner for the agent image; if it does not
   fit, a self-hosted runner or a larger runner is the fallback.
3. Whether to offer `nanoclaw-sandbox` once `sysbox-runc` is detected on the
   host.

# 5. Constraints

- POSIX `sh`, `docker` and `curl` on the host, as the launcher; no Node, pnpm
  or git.
- The gateway's management API is never reachable from agent containers
  (runbook 3); the launcher creates no other network membership.
- No secret in `argus.env` or in Argus's `docker inspect`-visible environment;
  the gateway's generated secrets live only in `onecli.env` (0600) and the
  gateway containers.
- Image versions move only with the Argus version; no `latest` in any config.

# 6. Implementation Consequences

Delivered as separate PRs, in this order:

1. This spec.
2. Release and CI: `deploy/nanoclaw-host` gains a build stage that produces the
   checkout (from `provision.sh`'s steps, split so they run in a Dockerfile),
   the agent image build, `.github/workflows/release.yaml` publishing both,
   and the quality CI build check. [Releases](releases.spec.md) 1 lists them.
3. Launcher: `worker setup|status|remove`, `ARGUS_WORKERS`, extra mounts in
   `up`, the update and rollback steps (3.5), tests against the fake `docker`
   in `scripts/test_launcher.py`. [Launcher](launcher.spec.md) links here.
4. API and dashboard: `ARGUS_LAUNCHER=1` messages (3.6).
5. Launcher: the first-run question and `--workers|--no-workers` (3.7), and
   `argus uninstall` ([launcher](launcher.spec.md) 3.9). The README
   Quickstart drops the `argus worker setup` line and documents
   `argus uninstall`.

The runbook keeps the manual route and links here for the launcher one.

## Verification boundary

- Commands: `uv run pytest scripts/test_launcher.py`; the release workflow on a
  tag; `make check`.
- Verified: (per PR) launcher logic against a fake `docker`; images build in CI.
- Not verified: on a clean Linux machine, `argus worker setup`, a task run end
  to end by a Claude account, `argus update` across two releases with workers,
  `argus worker remove --purge` and `argus uninstall` leaving nothing behind. To be done by hand
  on the first release that ships all four parts and recorded here.
