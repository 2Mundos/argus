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
| Reply | plain message → progress note (at most one per 10 s, coalesced); `COMPLETE: …` → complete; `FAIL: …` → fail; `ASK: …` → ask a question (the task waits for an answer); `ASSIST: [resource:] …` → ask the orchestrator for a missing tool/environment; `ASK on <task-id>: …` is accepted for the task in flight only. The prompt carries the task's [continuation contract](../../docs/specs/task-continuation.spec.md) |

One task at a time per channel. A 409 from Argus (lease lost, attempt
superseded) drops the reply and the channel moves on.

## Install

```bash
integrations/nanoclaw/install.sh /path/to/nanoclaw      # copies files, appends one import
cd /path/to/nanoclaw
pnpm run build && pnpm exec vitest run src/channels/argus.test.ts
pnpm exec tsx scripts/init-argus-agent.ts --agent-name "Worker"   # agent group + wiring
```

The install hook also patches the OneCLI gateway contribution so agent containers receive
`GIT_SSL_CAINFO` pointing at the same trusted CA bundle as `SSL_CERT_FILE` (Git ignores the
latter), and extends the Codex env allowlist so spawned shells keep `GIT_HTTP_PROXY_AUTHMETHOD=basic`
and `GIT_TERMINAL_PROMPT=0` with the proxy/CA (provider secrets stay blocked). Gitea repository
leases use OneCLI path `/{owner}/{repo}.git/*` for smart-HTTP injection while the lease is held.
Operator `docker exec` Git probes do not substitute for a full native worker checkout under
Codex; see [worker lifecycle spec 3.8](../../docs/specs/worker-lifecycle.spec.md).

### Codex sandbox profile (default on for Codex agents)

`install.sh` also installs the Codex sandbox profile
(`argus-codex-sandbox.ts`, patches to the driver, the spec type and `composeSessionSpec`, and the
seccomp JSON under `<checkout>/security/`). For native Codex agent containers it defaults to
`codex-bwrap` when `ARGUS_CODEX_SANDBOX_PROFILE` is unset; set `off` only when architecture cannot
enforce AppArmor/seccomp (e.g. private inner Docker). The AppArmor profile must be loaded by an
operator first; before each profiled spawn the driver proves on the daemon that the profile is
_enforcing_ (a disposable, credential-free probe container), and it refuses to adopt an existing
container that lacks the profile. See the [spec](../../docs/specs/codex-sandbox-profile.spec.md) and the
[runbook](security/RUNBOOK.md); the profiles are still candidates. Verify offline with
`security/scripts/verify-install.sh <nanoclaw git checkout>` (`UPGRADE_FROM=<installed checkout>` to prove an
in-place upgrade).

Then start the NanoClaw host with the Argus environment (what an Argus `plain`
runtime container receives):

```bash
ARGUS_URL=http://argus:8000 ARGUS_BOOTSTRAP_TOKENS=argus_boot_… \
ARGUS_CAPABILITIES=python,git pnpm start
```

Without `ARGUS_URL` the channel is simply off.

### Updating an installed copy

Nothing here updates an installed NanoClaw by itself. The channel (`src/channels/`)
runs in the host from the checkout's `dist/`, and agent containers mount the
runner source (`container/agent-runner/src`) read-only from the checkout, so a
change to either needs only a reinstall and a build of the checkout, not a new
agent image:

```bash
integrations/nanoclaw/install.sh ~/.local/share/argus-nanoclaw/src   # idempotent
cd ~/.local/share/argus-nanoclaw/src
pnpm run build && ~/path/to/argus/integrations/nanoclaw/install.sh . >/dev/null   # restores JS assets tsc drops
pnpm exec vitest run src/channels/
```

Rebuild the agent image (`provision.sh`, `ARGUS_NANOCLAW_ENGINES=1` on an engines
node) only when the image's own inputs change (`container/Dockerfile`, CLI tools,
`argus-delegate`, `gitea-api.mjs`). Then recreate the worker instances: running
hosts keep the old channel and running agent containers the old runner. The
runner hooks' own tests run with the image's Bun:
`cd container/agent-runner && bun test src/providers/claude-rate-limit.test.ts
src/providers/claude.rate-limit.test.ts`.

## Run it as an Argus runtime (`nanoclaw`)

For the guided `nanoclaw` template, `integrations/nanoclaw/provision.sh` does the
user-owned preparation below (pinned commit, build, agent image archive) and
prints the `ARGUS_NANOCLAW_*` environment; see
[the node setup runbook](../../docs/runbooks/2026-10-16.nanoclaw-worker-setup.ref.md).

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
# Required for any socket-mounted NanoClaw pool (templates and raw PUT alike):
# export ARGUS_NANOCLAW_ALLOW_DOCKER_SOCKET=true
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
  directory, never its siblings'. Argus creates the instance directory (owned by
  `user`) on the host when the daemon shares this filesystem; otherwise a
  one-shot container mounts only the parent directory and runs `mkdir`/`chown`
  as argv (no shell).
- **Socket opt-in.** `ARGUS_NANOCLAW_ALLOW_DOCKER_SOCKET=true` on the Argus
  process is required before any pool that mounts the Docker socket can be
  upserted (`403 docker_socket_not_allowed` otherwise). Sandboxed pools
  (`docker.runtime`) do not need it and must leave `nanoclaw.docker_socket`
  unset.
- **Hardened host containers.** Argus starts every runtime container with
  `--cap-drop=ALL`, `no-new-privileges`, a read-only root filesystem (tmpfs for
  `/tmp` and friends) and a PID limit.
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

## Live task log

While `argus-delegate` runs claude (`stream-json`), it turns the stream into short,
redacted lines plus a keepalive when silent, and appends them as JSON lines to
`argus-task-log.jsonl` in the agent group directory (`/workspace/agent/` in the
container; same mount as the model-policy snapshot). The delegate never has the Argus
token. The channel polls that file and posts batches to `POST /api/tasks/{id}/log`
with `source: "delegate"`, backing off on 429 and stopping on a stale attempt. See
[task-live-log.spec.md](../../docs/specs/task-live-log.spec.md) section 3.7. Overrides
for tests: `ARGUS_TASK_LOG_FILE`, `ARGUS_LIVELOG_FLUSH_S`, `ARGUS_LIVELOG_KEEPALIVE_S`,
`ARGUS_LIVELOG_TICK_S`. Executors other than claude only send keepalives.

## Accounts: leasing and switching

With `ONECLI_URL` set (the fleet always sets it) the channel takes its credential
from Argus's account pool instead of the one in the OneCLI vault. Argus ranks
**every** usable account by priority, across providers; there is no pool or
worker provider list (the retired `account_providers` / `executor_providers` /
`ARGUS_ACCOUNT_PROVIDERS` / `ARGUS_EXECUTOR_PROVIDERS` are ignored, see
[global account selection](../../docs/specs/account-selection.spec.md)):

1. After claiming a task it leases an account (`POST /api/accounts/lease`, bound
   to that task id and attempt, and stating only the providers this installed code can apply) and
   writes the credential into OneCLI: a secret `argus-<agent group id>` (with
   `-openai` / `-cursor…` suffixes for other providers) for the provider's host, assigned to that group's OneCLI agent in **selective** mode
   so it is the only secret the agent can use. The credential reaches the vault
   from the host process; the agent container never sees it.
2. When a run is blocked by the provider (the agent's failure notice reads
   `Rate limit …` / `Out of credits`, with the reset time when given), it reports
   the account exhausted. Argus marks it and returns next-account metadata only.
   The channel obtains the credential through the task-bound lease endpoint,
   `PATCH`es the vault secret's value, and re-sends the task to the agent. A
   switch needs no restart: OneCLI applies a changed value to the next
   request. The channel re-evaluates accounts only for a claimed task (at most
   once a minute), so recovered or higher-priority accounts are considered at a
   safe point; running work is never rotated.
   For a Claude subscription account, Claude Code withholds
   `CLAUDE_CODE_OAUTH_TOKEN` from its Bash tool, so `install.sh` also mounts a
   read-only gateway stub (`/home/node/.config/argus/claude-gateway.json`, sentinel
   only) from which `argus-delegate claude` restores the variable for its own
   child when the proxy and CA match; see
   [global account selection](../../docs/specs/account-selection.spec.md).
   Claude Code's own `You've hit your session|usage|weekly limit · resets 2am (UTC)`
   counts too, until the next such UTC time. In the agent runner, `install.sh`
   copies `overlays/claude-rate-limit.ts` and hooks `claude.ts` and `poll-loop.ts`
   (`patch-claude-rate-limit.py`, which stops the install if NanoClaw's code
   moved): a failed Claude turn whose result is exactly that one line, or that
   the SDK marked `rate_limit`, reaches the channel as the generic "The agent run
   failed" notice plus the structured `providerFailure`
   `{provider:"claude", kind:"rate_limit", resets_at?}`; no other upstream text
   leaves the container. (A runner without NanoClaw's add-codex
   `providerFailure` field gets only the `claude.ts` hook and the line itself is
   the notice.) A generic notice also counts when the agent log tail (optional
   `agentLogTail` hook of the channel config) holds `Rate limit [..] (resets ..)`
   or that line. This host does not wire the hook: the channel has no handle on
   the agent container's log or transcript, and the runner hook already turns the
   provider's own signal into the structured field, which is all the log would
   be read for. The task is resent on the same attempt and no failure is
   reported. The channel logs `account_failover`.
3. With no replacement, Argus blocks the task as `awaiting_quota` (with
   `refill_at`) and the channel lets go of it; it leases again (and claims) once
   an account is usable. When it cannot lease at claim time it calls
   `POST /accounts/awaiting-quota` so ready tasks show as waiting too.
   A Codex usage limit takes the same path: the runner overlay
   `overlays/codex-usage-limit.ts` (installed with the Claude one) yields
   `providerFailure {provider:"codex", kind:"rate_limit", notice?, reset_text?}`,
   the plain sentence ("You've hit your usage limit … try again at …", either
   apostrophe) and the structured `usageLimitExceeded` error are classified the
   same, and the provider's reset stays words (`reset_status`, `reset_text`), never
   a guessed time. The report also carries `reason_code` and `source`. If the old
   runtime cannot be stopped after a switch the channel fails the task with
   `reason=runtime_switch_failed` (an explicit block, no attempt spent) and
   recycles before it claims again; a claim refused `lease_account_unusable`
   makes it re-lease and recycle. After applying a switch it reports
   `POST /engines/selected` with `task_id`/`attempt`.
4. Any other failure notice now fails the attempt with its text, so Argus retries
   it (up to `max_attempts`) instead of leaving it claimed.

Results: a `COMPLETE:` longer than Argus's 10 000-character summary limit is attached
whole as the artifact `result-attempt-<n>.md` and summarized (never truncated
silently); files the agent attaches to any reply are uploaded as artifacts too and
listed in the completion; permanent rejections fail the attempt with the real reason
instead of leaving it claimed. See worker lifecycle spec 3.8.

Credential document fields: `api_key`, or `oauth_token` for a subscription; the
shape of Claude Code's own credentials file, `claudeAiOauth.accessToken`, is also
accepted. That access token is short-lived: if its `expiresAt` has passed the lease
is refused with a clear message (no value is logged) and the account must be
renewed in Argus, because the channel does not refresh OAuth tokens.
The credential stays in OneCLI's vault after the agent leaves; the vault is the
credential store by design.

## Orchestrating over MCP

An orchestrator token connected to Argus's `/mcp` endpoint gets the inbox
journey as native tools ([MCP operations](../../docs/specs/mcp-operations.spec.md)):
`get_inbox` lists what waits (blocked, in review, awaiting approval) with
decision and artifact ids; `list_decisions(view="detail")` or `get_decision`
reads the questions; `list_artifacts`, `verify_artifact` and `read_artifact`
check evidence; `answer_decision` or `answer_decisions` acts (give each answer a
`request_id`, so a retry is replayed, not refused); `get_tasks(ids)` reads the
effects back.
Lists are paged: `total` counts every match; follow `next_cursor` while
`has_more`. For the fleet, `inspect_pool` shows what each instance is proven to
run (each fact's `source`, or `unknown`); `recycle_instance` and `drain_agent`
never interrupt claimed work; admins change options with `update_pool`, passing
the pool's `version`; `wait_for_events` waits for the effect instead of polling.
If a tool is missing, say which and stop; do not script REST around it.

The channel reports its `runtime` on every heartbeat: `nanoclaw/<version>` from
the host's `package.json`, the engine the leased account selected, and the
executor lease's provider. Parts it does not know are sent as null and shown as
`unknown`.

## Not done yet

- No token usage is reported back (NanoClaw does not expose it to the channel).
- A task whose model call fails stays claimed while the host heartbeats, until
  Argus's wall-clock timeout (`timeout_s`, default 6 h; see the spec) expires the
  attempt. NanoClaw's "agent run failed" notice arrives as a progress note; cancel
  the task to end it sooner. The agent container is not interrupted when its
  attempt expires; the channel just drops its replies and claims the next task.
- Replies are matched by prefix, which an agent can get wrong; an MCP tool set
  would be sturdier but would put an Argus token near the agent.
