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
  the instance's own directory under `instances_root` and the Docker socket at the
  same absolute path inside the host container. A host sees only its own
  directory, never its siblings'. Argus creates it (owned by `user`) with a
  one-shot container from the same image before starting the host.
- **One working directory per instance** (`<instances_root>/<instance id>`): its
  own `data/`, `groups/`, `store/`; code and the files NanoClaw mounts into agents
  are symlinked from the install. Directories are kept after an instance ends.
- **Short `instances_root`** (at most 56 characters): NanoClaw binds Unix sockets
  under it and the OS limits socket paths to about 107 bytes; a longer path fails
  with a misleading `EADDRINUSE`. Argus rejects longer roots.
- **One agent per instance, by design** (`agents_per_instance` must be 1): agents do
  not share a host process, working directory or vault identity, for isolation.
- **Descendants.** Agent containers carry NanoClaw's labels, not Argus's. Argus
  finds them by `nanoclaw-group-folder` (`argus-<pool>-<instance id prefix>`), stops
  them with their host, keeps the instance un-terminated until they are gone, and
  removes orphans.
- **The Docker socket is root-equivalent** on that Docker host. Use the sandboxed
  mode below to avoid mounting it.
- **Upgrade tripwire.** For a fresh working directory the entrypoint stamps
  NanoClaw's upgrade marker with commit/tree `unknown` (its documented no-git
  case); an existing marker is never overwritten.

## Sandboxed mode (sysbox)

Set `docker.runtime` (e.g. `sysbox-runc`) and the host gets **no** Docker socket:
the image runs its own `dockerd` inside the sandbox runtime, so a compromised host
reaches neither the Docker host nor its sibling instances.

```json
"docker": {"runtime": "sysbox-runc", "extra_hosts": ["host.docker.internal:host-gateway"]},
"nanoclaw": {"install_dir": "...", "instances_root": "/srv/nc-i",
             "agent_image": "nanoclaw-agent-v2-008685e0:latest",
             "agent_image_archive": "/srv/nanoclaw-agent.tar"}
```

- The Docker host needs [sysbox](https://github.com/nestybox/sysbox) installed and
  registered as a runtime. `nanoclaw.user`/`group_add` must be empty (root inside
  the sandbox is unprivileged outside it).
- The inner daemon starts empty. `agent_image_archive` (a `docker save` file on the
  Docker host, mounted read-only) is loaded on start; without it `agent_image` is
  pulled from its registry.
- **Gateway reachability.** Inside the sandbox `host-gateway` would be the instance
  itself, so the entrypoint starts the inner daemon with `--host-gateway-ip` set to
  what the instance resolves `host.docker.internal` to (the pool's `extra_hosts`
  entry): agent containers then reach OneCLI on the real Docker host.
- Verified: pool validation, the container spec, the entrypoint (inner daemon
  starts, image loads, NanoClaw boots), and the gateway routing (an inner container
  was refused at `host-gateway` before the flag and reached a host listener after),
  on native Linux using `--privileged` as a stand-in. **Not verified:** under real
  sysbox, which is not installed on any host I can reach.

## Verified with a real model (2026-10-14)

Through Argus-managed containers, with an Anthropic credential in the OneCLI vault:

| Run | Result |
| --- | --- |
| `Say pong` | claimed, agent container spawned, `COMPLETE: pong` → task `succeeded`, 10 s from claim to completion, attempt 1 |
| `Pick the header colour` | agent sent `ASK:` → task paused, a `question` decision opened (6 s) → answered "Teal." → re-claimed as attempt 2 with the clarification → `COMPLETE: Teal`, no failures |
| scale the pool to 0 | the idle agent was drained, left, and the host container exited; instance `terminated` ("all agents left"), nothing left behind |

Drain behavior: once Argus marks the agent `draining` and no task is in flight,
the channel leaves Argus, stops claiming, and signals the NanoClaw host to shut
down (SIGTERM) so the instance ends. Under an Argus instance this is on by
default; set `ARGUS_EXIT_ON_DRAIN=false` to keep the host running.

## Accounts: leasing and switching

With `ARGUS_ACCOUNT_PROVIDERS=anthropic` (pool option `account_providers`) the
channel takes its credential from Argus's account pool instead of the one in the
OneCLI vault:

1. Before claiming work it leases an account (`POST /api/accounts/lease`) and
   writes the credential into OneCLI: a secret `argus-<agent group id>` for
   `api.anthropic.com`, assigned to that group's OneCLI agent in **selective** mode
   so it is the only secret the agent can use. The credential reaches the vault
   from the host process; the agent container never sees it.
2. When a run is blocked by the provider (the agent's failure notice reads
   `Rate limit …` / `Out of credits`, with the reset time when given), it reports
   the account exhausted. Argus returns the next account (same provider first),
   the channel `PATCH`es the vault secret's value, and re-sends the task to the
   agent. A switch needs no restart: OneCLI applies a changed value to the next
   request.
3. With no replacement, Argus blocks the task as `no_account` and the channel
   lets go of it; it leases again (and claims) once an account is usable.
4. Any other failure notice now fails the attempt with its text, so Argus retries
   it (up to `max_attempts`) instead of leaving it claimed.

Credential document fields: `api_key`, or `oauth_token` for a subscription.
The credential stays in OneCLI's vault after the agent leaves; the vault is the
credential store by design.

## Not done yet

- No token usage is reported back (NanoClaw does not expose it to the channel).
- A task whose model call fails stays claimed while the host heartbeats, until
  Argus's wall-clock timeout (`timeout_s`, default 6 h; see the spec) expires the
  attempt. NanoClaw's "agent run failed" notice arrives as a progress note; cancel
  the task to end it sooner. The agent container is not interrupted when its
  attempt expires; the channel just drops its replies and claims the next task.
- Replies are matched by prefix, which an agent can get wrong; an MCP tool set
  would be sturdier but would put an Argus token near the agent.
