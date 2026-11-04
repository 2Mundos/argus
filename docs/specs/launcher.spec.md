# Launcher

# 1. Purpose and Scope

Running Argus on one machine takes one install line and one command, and
upgrading it takes one command that cannot leave the operator without a working
install. `argus` is a POSIX shell script on the host that wraps `docker`:

```bash
curl -fsSL https://github.com/2Mundos/argus/releases/latest/download/install.sh | sh
argus up            # start (first run: create config, key, volume, offer workers), print the sign-in URL and token
argus up --docker   # same, plus local Docker pools (mounts the Docker socket)
argus update        # back up, upgrade to the newest release, roll back if it does not become ready
argus uninstall     # remove everything the launcher created (--keep-data keeps the data)
```

It covers the single-node install of [single-node](single-node.spec.md): one
container, one named volume, published on loopback. It replaces the multi-line
`docker run`, the token polling loop, the `compose.docker.yaml` environment
detection and the manual age key setup as the documented way to run a release.
The commands:

| Command | Does |
| --- | --- |
| `argus up [--docker\|--no-docker] [--port N] [--age-key FILE] [--version vX.Y.Z] [--workers\|--no-workers] [--no-tray]` | Writes the given options to the config, (re)creates the container from it at the configured version, waits for `/readyz`, prints the URL and, when one was generated, the admin token (3.3), and starts the desktop tray when available (3.10). `--version` and `--workers\|--no-workers` are accepted only on the first run (3.8). |
| `argus update [vX.Y.Z] [-y]` | The only way to change the version of an existing install (3.4). |
| `argus backup` | Writes an online SQLite backup into the volume (3.5) and prints its name. |
| `argus restore <name>` | Stops the container, backs up the current database, restores that backup, starts the version it was taken under (3.5). |
| `argus status` | Version, container state, `/readyz`, config path, the newest backups. |
| `argus tray [--stop]` | Manually start or gracefully stop the desktop tray ([desktop tray](tray.spec.md)); normally started automatically by `up`, requires `uv` on the host. |
| `argus token` · `argus logs [-f]` · `argus down` | Print the admin token · container logs · stop and remove the container (never the volume). |
| `argus unlock` | Removes a stale lock left by a command that died (3.7). |
| `argus worker setup\|status\|remove` | Workers on this machine: published worker images, the OneCLI gateway, Docker access ([local workers](local-workers.spec.md)). The first `up` offers `setup`; these add or remove workers later. Mutating subcommands take the lock (3.7). |
| `argus uninstall [--keep-data] [-y]` | Removes the containers, images, volumes, files and launcher this install created, after listing them and asking (3.9). |

# 2. Non-Goals

- No automatic or scheduled upgrades. A release with a migration is one-way
  ([releases](releases.spec.md) 3.3); every upgrade is started by the operator.
- No update check inside the Argus process. The API makes no call to GitHub;
  only `argus update` does, when the operator runs it.
- No replacement for Compose or Kubernetes deployments. `deploy/compose*.yaml`
  stay for building from a clone; the launcher runs published images only.
- No TLS, reverse proxy or exposure beyond loopback (and the Docker bridge
  gateway with `--docker`). Exposing Argus stays the operator's proxy.
- No native Windows server launcher. WSL 2 and macOS (Docker Desktop) are
  supported; `argus tray` in WSL launches the native Windows tray
  ([desktop tray](tray.spec.md)).
- No downgrade command. Going back is `argus restore` of a pre-upgrade backup,
  which runs the image the backup was taken under (3.5).

# 3. Decisions Made

## 3.1 The launcher and release notes ship inside the image

The image carries `/app/bin/argus` (the launcher) and `/app/releases/`
(`docs/releases/*.ref.md`). The launcher therefore always matches its image
version, and the notes for every version are available offline. `install.sh`
resolves the newest tag (3.4), or takes `ARGUS_VERSION=vX.Y.Z` to pin one,
pulls that image and copies the launcher out of it into `~/.local/bin/argus`
(or `$ARGUS_BIN_DIR`); it installs nothing else. The only network calls besides
`docker pull` are the newest-tag lookup and fetching `install.sh` itself.

`install.sh` is fetched from the newest release, not from `main`: the release
workflow attaches it to every release as an asset, so
`releases/latest/download/install.sh` is the copy that shipped with the newest
tag. The README's install line uses that URL and names no version; pinning is
an optional note (`ARGUS_VERSION`, `up --version`, `update vX.Y.Z`).

## 3.2 One config file, rewritten by flags

`~/.config/argus/` (or `$ARGUS_HOME`, mode `0700`) holds:

- `argus.env`: `ARGUS_IMAGE` (default `ghcr.io/2mundos/argus`),
  `ARGUS_VERSION` (a pinned `vX.Y.Z`, never `latest`), `ARGUS_PORT` (8000),
  `ARGUS_CONTAINER` (`argus`), `ARGUS_VOLUME` (`argus-data`), `ARGUS_DOCKER`
  (`0`/`1`), and `ARGUS_EXTRA_ENV`, a file of extra `-e` variables the operator
  edits by hand (for example Litestream). Like any `docker run -e`, these are
  visible in `docker inspect`; the launcher's own secrets (3.2, 5) never go there.
- `age-key.txt`: the age identity. `--age-key FILE` copies an existing
  identity in; otherwise the first `up` creates one with the image's
  `age-keygen`, but only when the volume has no secrets file yet (3.8). Mode
  `0644` inside the `0700` directory, so the container's `nobody` user can read
  the read-only bind mount and other host users cannot reach it. Its public key
  is passed as `SOPS_AGE_RECIPIENTS`.

Flags on `up` change the file; every later command uses only the file. The
defaults match the README's `docker run`, so an existing install can be adopted
without moving data (3.8).

## 3.3 `up` derives the Docker settings

`up` removes the container named in the config (if there is one) and runs it
again from the config. The volume holds all state, so this is how every change
is applied. Without `--docker` the container gets `-p 127.0.0.1:PORT:8000`, the
volume, the age key and the extra env, and no `ARGUS_PUBLIC_URL`, as today.
With `--docker` it also gets:

- the socket `/var/run/docker.sock`, and `--group-add` with the socket's group
  ID as seen from inside a container (`stat` run in the Argus image), which is
  correct on Linux and Docker Desktop alike;
- `--add-host host.docker.internal:host-gateway` and
  `ARGUS_PUBLIC_URL=http://host.docker.internal:PORT`;
- on a Linux engine (`docker info` does not report Docker Desktop), a second
  publish on the `bridge` network's gateway address, as `compose.docker.yaml` does.

`up` waits for `GET /readyz` to return 200, at most `ARGUS_READY_TIMEOUT`
(default 300 s; migrations run at start), and exits non-zero with the last 50
log lines if that runs out or the container exits first. Readiness does not
depend on the admin token: Argus writes `/data/admin-token` only when it
generates one (no `ARGUS_ADMIN_TOKEN` preset and no live admin token yet). `up`
and `token` print the file when it exists and otherwise say that an existing or
preset admin token applies. The file is never waited for.

## 3.4 `update` backs up, upgrades, and rolls back on failure

1. Resolve the target: the argument, or the newest release (the tag in the
   `Location` of `https://github.com/2Mundos/argus/releases/latest`). The same
   version as now is a no-op; an older version is refused (2).
2. `docker pull` the target and print the **Upgrading** section of every notes
   file in its `/app/releases/` newer than the current version and up to the
   target, so skipped releases' migrations are listed too.
3. Ask for confirmation (`-y` skips it).
4. Replace the installed launcher with the target image's `/app/bin/argus`
   (keeping the previous one as `argus.prev`; write to a temporary file in the
   same directory, then rename) and `exec` it, so the steps below run with the
   new version's container settings. The lock (3.7) is held across the `exec`.
5. Record the current image ID and stop the container (`docker stop`, so Argus
   shuts down cleanly). Only then take the backup, offline, with that image in a
   one-off container on the volume (3.5). No write can land after the snapshot,
   so a rollback loses nothing that was acknowledged.
6. Set `ARGUS_VERSION` to the target and run the `up` steps (3.3).
7. If it does not become ready (3.3), roll back: remove the new container,
   restore the backup with the recorded image, start that image, and restore
   `ARGUS_VERSION` and `argus.prev`. Print the failed container's last 50 log
   lines and exit non-zero.

Argus is down from step 5 until the new version is ready; that window is the
cost of a lossless rollback.

## 3.5 Backups live in the volume

A backup is `/data/backups/argus-<UTC timestamp>-<version>.db` (so names sort
oldest first), written by
SQLite's backup API (`python -c` in an Argus container), so it sits under the
volume's ownership. `argus backup` runs it online in the running container: it
is consistent, but writes after it are not in it, so it is for safekeeping, not
for the rollback in 3.4, which backs up offline. The launcher keeps the newest
5 and deletes older ones. `restore` stops the container, takes an offline
backup of the current database (so the restore itself can be undone), copies
the chosen backup over `/data/argus.db`, removes the `-wal` and `-shm` files,
and starts the image whose version the backup name records (pulling it if
needed), then sets `ARGUS_VERSION` to it. Backups cover the database only; the
artifact files in the volume are not rolled back. With Litestream configured,
the replica keeps working (Litestream starts a new generation after a restore).

## 3.6 What the launcher never touches

It acts on the one container and volume named in its config, and on the worker
containers, network and volumes it names itself ([local workers](local-workers.spec.md) 3.3).
Outside `uninstall` (3.9) it never removes the volume and never prunes images,
so the old image stays available for a rollback or restore. It never stops,
removes or prunes anything it did not name, `uninstall` included.

## 3.7 One mutating command at a time

`up`, `update`, `backup`, `restore`, `down` and `uninstall` take an exclusive lock for their
whole run: config changes, backups, container replacement, restore and
rollback. The lock is `mkdir $ARGUS_HOME/lock` (atomic and portable; `flock`
is not on macOS) with the holder's PID inside. A second command fails at once
and names the holder; a lock whose PID is not running is reported as stale and
removed by `argus unlock`, never silently. `update` keeps the lock across its
`exec` (same PID). `status`, `token` and `logs` take no lock.

## 3.8 First install and adoption

A **first install** is when neither the configured container nor the volume
exists. Then `up` takes `--version` (default: the newest release) and may
create an age key.

On an **existing install**, the version changes only through `update` or
`restore`; `up --version` is refused and names `argus update`. When there is
no config yet but the container or volume exists, `up` **adopts** it, and
inspects before it removes anything:

- `ARGUS_VERSION` comes from the container's image (`org.opencontainers.image.version`
  label); a `latest` or unlabeled image is refused with the exact version to pass.
- The container must have only what the README's `docker run` gives it: the
  volume at `/data` and a loopback port. Any other environment variable (for
  example `SOPS_AGE_*`, `ARGUS_ADMIN_TOKEN`, `ARGUS_SECRETS_FILE`,
  `LITESTREAM_*`, `ARGUS_PUBLIC_URL`) or mount stops adoption with the list of
  what differs; the operator moves it into the config (`--age-key`,
  `ARGUS_EXTRA_ENV`, `--docker`) and runs `up` again, which then adopts.
- If the volume has a secrets file (`/data/secrets.sops.yaml`) and no
  `--age-key` was given, adoption stops: a new key could not decrypt it.
- A volume without a container has no version to read: `up` then requires
  `--version` with the release that last ran on it.

Nothing is removed until these checks pass. Adoption then stops the old
container and takes an offline backup (3.5) before starting the new one, so the
first launcher-managed start can be undone with `argus restore`.

The **first run** (a first install, or an adoption) also decides whether this
machine runs workers: `up` asks, or takes `--workers` / `--no-workers`
([local workers](local-workers.spec.md) 3.7). Later runs never ask again; the
answer is changed with `argus worker setup` or `argus worker remove`.

## 3.9 `uninstall` removes what the launcher created

`argus uninstall` is the way out of the install line. It acts only on names
the launcher wrote or derives from its config, and first prints every one of
them with what it holds (database size, backup count, whether a secrets file
exists), then asks for confirmation (`-y` skips it; without a terminal and
without `-y` it refuses). It removes, in this order:

1. Stop the desktop tray, if running, and wait for it to release its singleton
   lock before removing anything ([desktop tray](tray.spec.md)). Failure to stop
   it leaves the installation intact and asks the operator to quit it and retry.
2. The worker containers and network as `argus worker remove` does, if workers
   are set up, and the Argus container as `argus down` does.
3. The images the launcher pulled, by exact reference: Argus and the worker
   images at the configured and previous versions (from `argus.prev` and the
   backup names), and the pinned gateway images. An image another container
   still uses is left and reported; nothing is pruned.
4. The worker checkouts under `$ARGUS_DATA_HOME/nanoclaw`.
5. Unless `--keep-data`: the Argus volume (database, artifacts and the backups
   inside it), the gateway volumes, and `$ARGUS_HOME` (config, age key,
   `onecli.env`). The confirmation names the volume and says that its backups
   and the age key go with it, so `argus backup` cannot save them; copying a
   backup and the key out comes first.
6. The installed launcher and `argus.prev`, last.

With `--keep-data` the volumes and `$ARGUS_HOME` stay, so running the install
line and `argus up` again starts the same install at its configured version
(an existing install, 3.8), with workers if they were set up. A step that
fails is reported and the rest still run; then `$ARGUS_HOME` and the launcher
stay, and the command exits non-zero, so a second `uninstall` finishes the job.

## 3.10 `up` starts the desktop tray

After the server becomes ready, `up` starts the desktop tray in the background
on a desktop or in WSL 2 ([desktop tray](tray.spec.md)). It returns to the terminal
without waiting for the tray to exit. `--no-tray` skips this for one invocation;
headless sessions skip it automatically. Missing tray dependencies or startup
errors leave the server running and do not fail `up`. Repeated starts share one
tray per config directory. `down` leaves the tray open so its Up action can start
Argus again; the tray's own Up and Restart suppress another tray launch.

# 4. Open Questions

1. Engines other than Docker Engine and Docker Desktop (Podman, Colima,
   OrbStack). `host.docker.internal` and the socket path differ; until tested,
   `--docker` on them is unsupported and `up` says which engine it detected.

# 5. Constraints

- POSIX `sh` plus `docker` and `curl` on the host; no `jq`, Python or age on
  the host for terminal commands. Anything that needs more runs inside the Argus
  image, except the optional `tray` companion, whose Python and Qt environment is
  managed by `uv` on the desktop ([desktop tray](tray.spec.md)).
- The launcher never prints secrets except the admin token on `up` and `token`.
  It never passes the age key or token on a command line or in
  `docker inspect`-visible environment.
- Every step that removes or replaces something (container, database file,
  launcher) runs after the checks that could refuse it, under the lock (3.7).
- It changes nothing in the running process or schema; Argus behaves the same
  whether started by the launcher, `docker run` or Compose.

# 6. Implementation Consequences

- New `deploy/argus` (the launcher) and `deploy/install.sh`.
- `deploy/Dockerfile.api` copies `deploy/argus` to `/app/bin/argus`,
  `docs/releases/` to `/app/releases/`, and a checksum-pinned `age-keygen`
  next to `sops`.
- `scripts/test_launcher.py`: runs the launcher against a fake `docker` and
  `curl` on `PATH` that record calls and script responses. It covers config
  creation and adoption, `--docker` flags on a Linux engine and on Docker
  Desktop, an `update` that becomes ready, an `update` that fails and rolls
  back (image, database, version and launcher; the backup is taken after the
  stop), refusing a downgrade and `up --version` on an existing install,
  adoption refused for extra env, mounts or an unkeyed secrets file, `up`
  ready without an admin-token file, a second mutating command refused while
  the lock is held, backup pruning, `uninstall` with and without
  `--keep-data` (only named objects removed, an image still in use left,
  refused without a terminal and `-y`), and the first-run worker question
  (asked once; `--workers`, `--no-workers`, no terminal). `install.sh` runs against the same
  fakes: the newest release by default, `ARGUS_VERSION` when set.
- README Quickstart becomes the install line and `argus up`. "Run with Docker"
  keeps plain `docker run` and Compose as the manual route.
- [Single-node](single-node.spec.md) 6 names the launcher as the packaged run;
  [releases](releases.spec.md) 1 notes that the image carries the launcher and
  the notes.

## Verification boundary

- Commands: `uv run pytest scripts/test_launcher.py` (also run by
  `make check-changed` when `deploy/argus` or `deploy/install.sh` change);
  `make check`.
- Verified: launcher logic against a fake `docker` (unit level); the image
  contains `/app/bin/argus`, `/app/releases/` and `age-keygen` (CI image build
  and boot).
- Not verified: a real `argus update` across two published releases with a
  migration, on Linux Docker Engine and on Docker Desktop; to be done by hand
  on the first release that ships the launcher and recorded here.
