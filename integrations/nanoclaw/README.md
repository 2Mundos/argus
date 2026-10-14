# NanoClaw integration

An Argus worker channel for [NanoClaw](https://github.com/qwibitai/nanoclaw): it
makes a NanoClaw agent group claim tasks from Argus, work on them, and report
back. See [the research note](../../docs/research/2026-10-14.nanoclaw-integration.research.md)
and [worker lifecycle spec 3.8](../../docs/specs/worker-lifecycle.spec.md).

## What it does

The channel runs in the NanoClaw **host** process; the agent container never
holds an Argus token.

| Direction | Mechanism |
| --- | --- |
| Claim | registers with a bootstrap token, long-polls `/api/tasks/claim`, injects the task (spec, acceptance, pinned knowledge page bodies) as an inbound message |
| Liveness | heartbeats, renewing the lease on the task it holds; stops claiming when Argus reports `draining`; leaves Argus on shutdown |
| Reply | plain message → progress note (at most one per 10 s, coalesced); `COMPLETE: …` → complete; `FAIL: …` → fail; `ASK: …` → ask a question (the task waits for an answer) |

One task at a time per channel. A 409 from Argus (lease lost, attempt
superseded) drops the reply and the channel moves on.

## Install

```bash
integrations/nanoclaw/install.sh /path/to/nanoclaw      # copies files, appends one import
cd /path/to/nanoclaw
pnpm run build && pnpm exec vitest run src/channels/argus.test.ts
pnpm exec tsx scripts/init-argus-agent.ts --agent-name "Worker"   # agent group + wiring
```

Then start the NanoClaw host with the Argus environment (what an Argus `plain`
runtime container receives):

```bash
ARGUS_URL=http://argus:8000 ARGUS_BOOTSTRAP_TOKENS=argus_boot_… \
ARGUS_CAPABILITIES=python,git pnpm start
```

Without `ARGUS_URL` the channel is simply off.

## Run it as an Argus runtime (`nanoclaw`)

Argus can start NanoClaw hosts for you: a pool with `runtime: nanoclaw` runs the
image from [`deploy/nanoclaw-host`](../../deploy/nanoclaw-host) (Node + the docker
CLI + an entrypoint), which runs a NanoClaw checkout **bind-mounted from the
Docker host** and spawns its agent containers on that host's daemon.

Prerequisites, all on the Docker host:

1. A NanoClaw checkout, built (`pnpm install && pnpm run build`), with the channel
   installed (`install.sh`, above) and the OneCLI gateway set up
   (`--step gateway`, plus `--step gateway-auth` for a real credential).
2. The NanoClaw agent image on that daemon (`--step container`).
3. A **short** directory for per-instance working directories.

```bash
docker build -t argus-nanoclaw-host:dev deploy/nanoclaw-host
curl -X PUT $ARGUS/api/pools/nc -H "Authorization: Bearer $ADMIN" -H 'Content-Type: application/json' -d '{
  "runtime": "nanoclaw", "image": "argus-nanoclaw-host:dev",
  "desired_replicas": 1, "max_replicas": 3,
  "docker": {"extra_hosts": ["host.docker.internal:host-gateway"]},
  "nanoclaw": {
    "install_dir": "/home/me/nanoclaw",
    "instances_root": "/srv/nc-i",
    "agent_image": "nanoclaw-agent-v2-008685e0:latest",
    "user": "1000:1000", "group_add": ["<gid of /var/run/docker.sock>"]
  }
}'
```

How it works and what to know:

- **Identical paths.** NanoClaw hands bind-mount paths to Docker, which resolves
  them on the daemon's filesystem. Argus mounts `install_dir` (read-only),
  `instances_root` and the Docker socket at the same absolute path inside the host
  container.
- **One working directory per instance** (`<instances_root>/<instance id>`): its
  own `data/`, `groups/`, `store/`; code and the files NanoClaw mounts into agents
  are symlinked from the install. Directories are kept after an instance ends.
- **Short `instances_root`** (at most 56 characters): NanoClaw binds Unix sockets
  under it and the OS limits socket paths to about 107 bytes; a longer path fails
  with a misleading `EADDRINUSE`. Argus rejects longer roots.
- **One agent per instance** (`agents_per_instance` must be 1).
- **Descendants.** Agent containers carry NanoClaw's labels, not Argus's. Argus
  finds them by `nanoclaw-group-folder` (`argus-<pool>-<instance id prefix>`), stops
  them with their host, keeps the instance un-terminated until they are gone, and
  removes orphans.
- **The Docker socket is root-equivalent** on that Docker host.
- **Upgrade tripwire.** For a fresh working directory the entrypoint stamps
  NanoClaw's upgrade marker with commit/tree `unknown` (its documented no-git
  case); an existing marker is never overwritten.

## Not done yet

- Account leasing and the OneCLI credential switch (verified to work on a running
  process; not wired into the channel).
- Several agents per NanoClaw host (`agents_per_instance > 1`).
- A task whose model call fails stays claimed while the host heartbeats, until
  Argus's wall-clock timeout (`timeout_s`, default 6 h; see the spec) expires the
  attempt. NanoClaw's "agent run failed" notice arrives as a progress note; cancel
  the task to end it sooner. The agent container is not interrupted when its
  attempt expires; the channel just drops its replies and claims the next task.
- Replies are matched by prefix, which an agent can get wrong; an MCP tool set
  would be sturdier but would put an Argus token near the agent.
