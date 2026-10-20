# Worker Lifecycle and Task Coordination Spec

Finalized from [the 2026-10-14 draft](../drafts/2026-10-14.worker-lifecycle.draft.md).
Everything here operates within the [lightweight single-node](single-node.spec.md)
principle.

# 1. Purpose and Scope

Define how Argus coordinates a fleet of agents:

- **Tasks**: what a unit of work is, its states, and how a worker claims, runs,
  and finishes one with only its current lease holder authorized to update it.
- **Agents**: how a worker or orchestrator registers, proves it is alive, and is
  declared lost.
- **Instances**: how Argus starts and stops the containers agents run in, at the
  request of the orchestrator or a human.
- **Accounts**: how agents share a pool of provider accounts and fall back when
  one is exhausted.
- **Decisions, results, and outputs**: human-in-the-loop gates, task kinds, and
  where progress, logs, and artifacts live.

Actors:

| Actor | Does |
| --- | --- |
| Orchestrator agent | Breaks goals into tasks, sets fleet size, reviews results. |
| Worker agent | Claims tasks it can do, works on them, reports results. |
| Human (dashboard) | Watches everything; answers decisions; can create/cancel tasks, scale pools, kill instances. |
| Argus | Holds the state, enforces the rules below, runs the reconciler and reaper. |
| NanoClaw worker (execution/delivery plane) | Dispatches implementation agents, maintains the task's isolated checkout and branch, runs project gates, commits, creates/updates its PR, monitors CI on the exact head, fixes findings, and returns structured completion to Argus. |

Argus is the control plane: it owns task creation/coordination, assignment and
leases, task states/progress/results, retry/cancel/block/unblock, and worker
and pool lifecycle/health. It may display or retain worker-reported delivery
metadata, but is not the central repository or PR operator. These boundaries do
not alter the task/lease semantics below. A task-result `review` gate and the
PR's own CI/review are distinct; neither implies the other. PR status must be
tied to the exact reported head SHA. Merge remains with a human or existing
repository policy unless a task or pinned delivery policy explicitly authorizes
it; Argus grants no automatic merge authority. When authorized, the NanoClaw
delivery worker may merge under the preconditions injected by `renderTask`
(required checks green on the exact head, P1–P3 findings resolved, no
force_merge or bypass); merge does not authorize manual Production operations.

# 2. Non-Goals

- Argus does not run agent logic, prompt models, or judge result quality. It
  stores state and enforces transitions; agents do the thinking.
- No general workflow engine (no loops, conditionals, timers in the task graph).
  Dependencies between tasks are the only control flow.
- No push-based dispatch. Workers pull; Argus never sends work into a container.
- No multi-tenancy in v1. One Argus deployment serves one fleet.
- No cross-host scheduling cleverness in v1 (bin-packing, affinity). One
  target per pool.
- No spend enforcement in v1. Usage is recorded (3.9), not capped.
- No log aggregation or search stack (see single-node 3.4).

# 3. Decisions Made

## 3.1 Data model

SQLite is the only store (single-node 3.2). JSON columns hold lists and
documents.

- **`pools`** — a named group of interchangeable workers: `name`, `target`,
  `runtime`, `image`, `capabilities` (json), `agents_per_instance`,
  `desired_replicas`, `max_replicas`, `config` (json: env, resources).
- **`instances`** — one container: `id`, `pool`, `provider_ref`, `state`,
  `created_at`, `terminated_at`, `exit_reason`.
- **`agents`** — one registered agent identity: `id`, `role`
  (`orchestrator` | `worker`), `instance_id` (nullable: an orchestrator may run
  outside Argus), `name`, `capabilities` (json), `state`, `last_heartbeat_at`,
  `ended_at`, `end_reason`.
- **`tasks`** — `id`, `parent_id`, `kind`, `title`, `spec` (Markdown),
  `acceptance` (json list), `input` (json), `required_capabilities` (json),
  `priority`, `state`, `requires_approval`, `review` (`none` | `human` |
  `orchestrator`), `assignee_agent_id`, `lease_expires_at`, `lease_ttl_s`,
  `attempt`, `failures`, `max_attempts`, `result` (json), `error`,
  `block_reason`, `created_by`, timestamps. A database `CHECK` enforces that an
  assignee and lease exist exactly while the task is `claimed`.
- **`task_deps`** — `(task_id, depends_on_id)`.
- **`task_kinds`** — 3.11.
- **`accounts`**, **`account_leases`** — 3.9.
- **`decisions`** — 3.10.
- **`tokens`** — hashed bearer tokens with scope, agent, and revocation time.
- **`events`** — `id` (integer primary key), `at`, `actor`, `subject_type`,
  `subject_id`, `kind`, `data` (json). Every state transition writes one row in
  the same transaction. The dashboard timeline, SSE resume, and audit trail are
  this table.

Large outputs are never stored in `result`; it holds artifact references (3.12).

## 3.2 Task state machine

```
                approve                 deps done           claim
 awaiting_approval ───▶ pending ─────────────▶ ready ─────────────▶ claimed
                          │                     ▲                   │
                          │                     │ lease expired /   │ complete
                          │                     │ fail, failures<max│
                          │                     ├───────────────────┤──▶ succeeded (review: none)
                          │                     │ review rejected   │──▶ in_review ──▶ succeeded
                          │                     └────── in_review ◀─┘
                          │                     ▲
                          │                     │ unblocked
                          │                  blocked ◀── needs input / no account
                          │
                          │   fail / expire (failures = max) ──▶ failed
                          └── cancel (any non-terminal state) ──▶ cancelled
```

- A task created with `requires_approval` starts in `awaiting_approval` and
  opens a decision (3.10); approval moves it on as if just created.
- A task with no unfinished dependencies enters `ready` directly.
- When a task `succeeded`, dependents whose dependencies are all done move to
  `ready` in the same transaction.
- If a dependency ends `failed` or `cancelled`, dependents stay `pending` and an
  event is raised for the orchestrator to decide (cancel, replace, or retry).
  Argus does not cascade cancellation on its own.
- `blocked` means the worker needs something it cannot get (an answer, an
  account, credentials). The lease is released; the task returns to `ready`
  when unblocked.
- With `review` set, completion moves the task to `in_review` and opens a
  decision. Acceptance moves it to `succeeded`; rejection returns it to `ready`
  with the reviewer's notes appended to `spec`, without counting a failed
  attempt toward `max_attempts`.
- Two counters, deliberately separate: `attempt` is bumped on every claim and
  never decreases, because it fences stale writers (3.3); `failures` counts only
  `fail` and lease expiry, and is what `max_attempts` limits. A task whose
  `failures + 1` reaches `max_attempts` on a failure goes to `failed`. Review
  rejections and unblocks lead to new claims (new `attempt`) without touching
  `failures`.
- Terminal states: `succeeded`, `failed`, `cancelled`.
- `POST /tasks/{id}/retry` (manager scope) is the one way out of `failed`, for
  when the environment changed and the work did not (provider quota, a
  credential, a missing binary). It is deliberately not a `transition` action:
  workers can never trigger it. Any other state answers 409 `task_not_failed`
  (`task is claimed, not failed`). It resets `attempt` and `failures` to 0 (the
  latter is what `max_attempts` limits), clears `error`, the assignee, lease,
  deadline and progress, keeps `max_attempts`, and returns the task to `ready`.
  Each retry is an explicit operator act and writes `task.retried` with the
  actor, the `previous_attempts` it discarded (which may exceed `max_attempts`)
  and the previous error, truncated to 500 characters.
- The transition table is one pure function, `argus_core.tasks.transition`,
  tested over every (state, action) pair.

## 3.3 Claiming: leases, not locks

- `POST /tasks/claim` with the worker's capabilities. In one write transaction,
  Argus selects the highest priority, oldest `ready` task whose
  `required_capabilities ⊆ agent.capabilities`, sets `claimed` and the assignee,
  increments `attempt` (initially zero), and sets the lease deadline to the
  current Argus time plus `lease_ttl`.
- The claimed task comes with `knowledge_refs`: pinned and best-matching
  knowledge pages ([knowledge](knowledge.spec.md) 3.4).
- Claim long-polls (up to 30 s) when nothing is ready, so idle workers do not
  hammer the API. Waiters are woken by the in-process event bus
  (single-node 3.3).
- Every worker mutation of a claimed task, including completion, failure,
  blocking, progress, subtask creation, and lease renewal, carries
  `(task_id, attempt)`. In the same transaction as the write, Argus checks that
  the authenticated agent is active (`busy` or `draining`), the task is
  `claimed`, its assignee matches that agent, its attempt matches, and
  `lease_expires_at` is strictly later than the current Argus time. A failed
  ownership check returns `409` without changing the task, even if the reaper
  has not processed expiry yet. Revoked credentials return `401`.
- Claims, worker writes, heartbeats, and reaping are serialized by the single
  writer: each runs in its own `BEGIN IMMEDIATE` transaction, rechecks state
  inside it, and reads the clock **after** the transaction has begun, so time
  spent waiting for the writer can never validate a lease that expired in the
  meantime. Renewal sets the deadline to that fresh time plus `lease_ttl` and
  cannot revive an expired lease. Leaving `claimed` clears the assignee and
  deadline in the same transaction as the transition and its event.
- These rules fence writes to Argus. Execution is at least once: an expired
  worker can continue external work while a retry runs. Workers must stop work
  when renewal is rejected or the last confirmed lease expires. External
  effects need destination-enforced idempotency using a stable operation key
  across attempts, or destination-enforced fencing; Argus cannot guarantee
  exactly-once effects in another system.
- **Attempt timeout.** A lease proves the holder is alive; it does not prove it is
  making progress, and a healthy holder renews it forever. So each attempt also
  has a wall-clock deadline (`timeout_at`) set at claim from the task's
  `timeout_s`, else its kind's `default_timeout_s`, else `ARGUS_TASK_TIMEOUT_S`
  (default 6 h; 0 disables). Neither renewals nor progress notes move it. The
  reaper expires an overdue attempt exactly like a lost lease: it counts as a
  failure (retried until `max_attempts`), the assignee is cleared, and the holder's
  next write gets `409`. The expiry event carries `reason: timeout` (other
  expiries: `lease_expired`, `agent_ended`).
- Defaults: `lease_ttl = 120 s`, heartbeat every 30 s, `max_attempts = 3`.
  Per-task and per-kind override allowed.

## 3.4 Agent liveness

- Stored agent states: `active → draining → gone`, plus `lost` from either live
  state. Whether an active agent is *busy* or *idle* is derived from the tasks it
  holds (`claimed_tasks`), never stored, so it cannot drift from the tasks table.
  The agent row is created at registration; there is no stored `registering`.
- An agent registers on boot (`POST /agents/register`) with a one-time
  bootstrap token injected into its container, and receives a long-lived agent
  token (3.6). Bootstrap tokens expire (default 1 h) and cannot authenticate
  any other request.
- Only `active` agents may claim. `active` and `draining` agents may write to
  tasks they hold; any other state fails the ownership check (3.3) with
  `agent_<state>`.
- One heartbeat call (`POST /agents/me/heartbeat`) refreshes the agent and
  renews the submitted list of `(task_id, attempt)` pairs using 3.3, plus its
  account leases (3.9). The response lists renewed leases with their deadlines
  and rejected pairs; a rejected pair does not renew that task or prevent other
  valid pairs from renewing. Agent liveness can refresh even when a task lease is
  rejected. Workers do not heartbeat per task.
- The **reaper** loop (every 10 s) independently expires task leases whose
  deadline has passed, including leases belonging to agents still sending
  heartbeats. It also marks agents with
  `last_heartbeat_at < now - 3 × interval` as `lost`, expires their leases
  (tasks go back to `ready` or to `failed` per 3.2), releases their account
  leases, revokes their agent and unused bootstrap tokens, and requests
  termination of their instances. Agent loss, credential revocation, task
  transitions, and events commit together in one write transaction. Provider
  calls happen after commit.
- `lost` is irreversible for that identity. A late heartbeat cannot restore it,
  renew a lease, or authorize another claim; the old instance cannot register
  again. A replacement gets a new instance id and bootstrap token.
- `draining` (`POST /agents/{id}/drain`): the agent finishes its current task,
  claims nothing new, then exits with `POST /agents/me/leave`, which ends the
  identity as `gone` with the same revocation and lease release as loss. Used
  for scale-down and for rolling a pool to a new image.
- Defaults: heartbeat every 30 s, `lost` after 90 s of silence, reaper pass
  every 10 s.

## 3.5 Provisioning: desired state + reconciler

Nobody "spawns a container" directly. The orchestrator or a human sets
`pools.desired_replicas`; a **reconciler** loop compares desired vs. live
instances and converges:

- Capacity counts every instance not confirmed terminated, including starting,
  draining, terminating, and unreachable instances. Reserve a slot in the
  database before each start; both `desired_replicas` and `max_replicas`
  constrain starts. An ambiguous start result retains its reservation and is
  resolved through provider lookup by the stable instance id before another
  start is attempted.
- Too few occupied slots → start instances through the pool's target.
- Too many → mark the newest idle agents `draining`; never kill a busy agent
  for scale-down.
- Instance whose agent is `lost` → mark the instance `terminating` and request
  `stop` with a bounded grace period followed by forced termination. Agent loss
  alone never records a container exit or frees its capacity slot. Retrying
  `stop` for the same provider reference is safe, including after an Argus
  restart. If the instance hosts multiple agents, marking it `terminating` also
  marks its other agents `lost`, revokes all its credentials, and expires their
  leases in the same transaction before stopping the shared host.
- Set an instance's terminal state and `terminated_at` only after a successful,
  authoritative provider query confirms it stopped or no longer exists. A
  timeout, connection failure, or stop acknowledgement is not confirmation.
  Until confirmed, retain the slot, retry termination, and surface the unresolved
  instance to the operator. Replacement for that instance waits for confirmation
  and then starts only if occupied capacity is below desired and the cap.
  Confirmation covers all instance-owned workloads, including NanoClaw agent
  containers (3.8). Runtimes must propagate the Argus instance id to descendants
  so the target can find and stop them even if their host has already exited.
- Container confirmed exited → revoke its agent and bootstrap tokens and expire
  any remaining leases in the same transaction as recording termination, then
  replace it if still below desired. An idle or draining instance follows the
  same confirmation rule before its slot is freed.
- `max_replicas` caps what an orchestrator can request. Raising the cap is a
  human-only action (guards against a runaway orchestrator).

The reconciler is an asyncio task in the Argus process (single-node 3.1); the
startup lock file guarantees there is only one. As built:

- **Instance states**: `reserved` (slot taken, container not confirmed) →
  `running` → `terminating` → `terminated`. Only a provider listing that no
  longer shows the container (or shows it exited) moves an instance to
  `terminated`.
- **Each pass, per pool** (every `ARGUS_RECONCILE_INTERVAL_S`, default 5 s):
  observe (list the pool's labeled containers), decide in one write
  transaction, then act (start/stop) after commit. If the target cannot be
  listed, the pool is marked `unreachable` and nothing about it changes that
  pass.
- **Starts**: containers are named `argus-<instance id>`, so a start is looked up
  before it is retried and can never produce a second container. Each attempt
  mints fresh bootstrap tokens (Argus stores only hashes) and revokes the
  previous unused ones. Failed starts retry with exponential backoff (5 s
  doubling, max 5 min) and mark the pool `failing` with the error. Nothing
  starts without `ARGUS_PUBLIC_URL` (how containers reach Argus).
- **Terminations** are begun when an agent of the instance is lost, when all its
  agents have left, when no agent registers within 5 minutes of the container
  running, on scale-down of an instance that has no agent yet, or by an admin
  (`POST /api/instances/{id}/terminate`). Exited containers are removed after
  they are confirmed. Containers carrying the pool's label that Argus does not
  track as live are stopped as orphans.
- **Scale-down** drops reserved instances first, then drains idle running
  instances (their agents go `draining`, leave, and the container then goes);
  instances with a claimed task are skipped.
- **Scaling API**: `POST /api/pools/{name}/scale`. Up to `max_replicas`,
  orchestrators and admins scale directly; above it an admin raises the cap
  along with it, while an orchestrator's request opens a `policy` decision for a
  human (approve raises the cap and scales). Pool definitions, including the cap,
  are admin-only (`PUT /api/pools/{name}`).
- **Crash loops**: three instances that exit within a minute of starting, inside
  10 minutes, hold new starts for 10 s, 20 s, 40 s, … (up to 5 min), and the
  pool is `failing` with the last exit reason. A single crash is replaced at once.
- **Descendants**: a runtime may own containers Argus did not start (NanoClaw's
  agents). `runtimes.descendant_label/descendant_owner` identify them; they are
  stopped with their instance, an instance stays `terminating` until none remain,
  and ones whose instance is not live are stopped as orphans.
- **Kubernetes target** (`target: kubernetes`, plain runtime only): one Pod per
  instance in one namespace, named `argus-<instance id>` and carrying the same
  `argus.*` labels, so every rule above (idempotent start by name, confirmation
  only by listing, orphan sweep, crash-loop backoff) applies unchanged.
  - **Explicit context.** `kubernetes.context` is required: a kubeconfig context
    name, or `in-cluster`. Argus never falls back to the kubeconfig's current
    context, so a pool cannot land on whichever cluster was last selected. The
    client never mutates global kubeconfig state.
  - **Pod shape.** `restartPolicy: Never` (replacing an instance is Argus's
    decision, not the kubelet's), `automountServiceAccountToken: false` (workers
    talk to Argus, not to the cluster), `allowPrivilegeEscalation: false`, CPU and
    memory limits equal to requests when set, plus optional `service_account`,
    `image_pull_secrets`, and `node_selector`.
  - **Status mapping.** Pending → created, Running → running (a pod being
    deleted still lists as running until it is actually gone), Succeeded/Failed →
    exited with the container's exit code. A pod that cannot start (for example
    `ErrImagePull`) is reported by the logs endpoint as its waiting reason.
  - **Needs**: Pods in the pool's namespace can reach `ARGUS_PUBLIC_URL`, and the
    credentials Argus uses can create/get/list/delete Pods and read their logs
    there (`deploy/kubernetes/rbac.yaml`, a namespace-scoped Role). The one-time
    bootstrap token is in the Pod's env, readable by anyone who can read Pods in
    that namespace; it is single-use and expires.
  - A NanoClaw host needs the node's Docker daemon and identical host paths, so
    the `nanoclaw` runtime is not available on this target.
- **Logs**: `GET /api/instances/{id}/logs?tail=N` reads the container's recent
  output live from the target; nothing is stored. Following logs in real time is
  a later addition.

**The `plain` runtime contract.** Argus starts the pool's image with:
`ARGUS_URL`, `ARGUS_BOOTSTRAP_TOKENS` (comma-separated, one per agent),
`ARGUS_INSTANCE_ID`, `ARGUS_POOL`, `ARGUS_CAPABILITIES`, plus the pool's `env`,
and labels `argus.managed`, `argus.pool`, `argus.instance_id`. The image
registers, heartbeats, claims, and leaves when drained or sent SIGTERM.
`examples/echo-worker` is the reference implementation (standard library only)
and the image the Docker integration test scales (`make test-docker`).

Provisioning has two independent axes, so N targets × M runtimes do not need
N×M implementations:

- **Target**: *where* a container runs. Two implementations cover the three
  required forms:
  - `docker`: the Docker Engine API. Local socket and remote host are the same
    code; only `DOCKER_HOST` differs (`unix://…`, `ssh://user@host`, or
    `tcp://…` with TLS).
  - `kubernetes`: one Pod per instance, created by Argus in a configured
    namespace (no CRD or operator in v1).
- **Runtime**: *what* runs in the container and how it is wired to Argus
  (image, env, mounts, how the agent learns its bootstrap token). v1 runtimes:
  `nanoclaw` (3.8) and `plain` (any image that speaks the Argus API itself;
  used for CI integration tests and custom workers).

```python
class Target(Protocol):
    async def start(self, spec: ContainerSpec) -> str: ...  # returns provider_ref
    async def stop(self, provider_ref: str, *, grace: timedelta) -> None: ...
    async def list(self, labels: Mapping[str, str]) -> list[ProviderInstance]: ...
    def logs(self, provider_ref: str) -> AsyncIterator[str]: ...


class Runtime(Protocol):
    def container_spec(
        self, pool: Pool, instance: Instance, bootstrap: str
    ) -> ContainerSpec: ...
```

A pool names one target and one runtime. `list()` filters on Argus labels
(`argus.instance_id`, `argus.pool`) so the reconciler finds containers that
died silently, and orphans Argus did not start. `start()` is idempotent by the
instance id in `ContainerSpec`; retries and ambiguous starts must resolve to one
instance on the target. `logs()` streams live container logs for the dashboard
(3.12).

## 3.6 Auth and scopes

Bearer tokens (`argus_boot_…`, `argus_agent_…`), 256-bit random, stored as
SHA-256 hashes in SQLite. An invalid token is always `401`, never a downgrade to
anonymous access. Three scopes:

| Scope | Can |
| --- | --- |
| `worker` | Claim, heartbeat, update/complete/fail/block its own tasks, report progress, ask questions, lease accounts, create subtasks under a task it holds. |
| `orchestrator` | Everything `worker` can, plus create/cancel any task, edit deps, answer `orchestrator` decisions, set `desired_replicas` up to `max_replicas`, drain agents. |
| `admin` (human) | Everything, including pool and account definitions, `max_replicas`, killing instances, revoking tokens. |

Every route requires a token except `GET /healthz` and `POST /agents/register`
(which carries a bootstrap token in its body). Any valid token may read tasks
and agents. Worker operations (claim, heartbeat, complete/fail/block, leave)
need an *agent* token, which orchestrators also hold; admin tokens are not
agents. Workers may create a task only as a subtask of one they currently hold.
A task with `review: human` can only be reviewed with an admin token.

Agent tokens are scoped to one agent and revoked when the agent becomes `lost`
or `gone`, or its instance terminates. Unused bootstrap tokens for that instance
are also revoked; revocation does not wait for an unreachable container to stop.

**Admin tokens.** No users or passwords in v1 (single-node 3.5):

- On first start with no live admin token, Argus generates one, writes it to
  `/data/admin-token` (mode 0600), and logs it once.
- `ARGUS_ADMIN_TOKEN` (at least 32 characters) presets one instead. It is
  registered the first time Argus sees it; once revoked it stays revoked even if
  the variable is still set.
- Admins mint and revoke further admin tokens (`/admin/tokens`). Revoking the
  last live admin token is refused, so an operator cannot lock themselves out.
- Dashboard login or an authenticating proxy in front of Argus can come later.

**The orchestrator** starts outside Argus: an admin mints a bootstrap token with
`role: orchestrator` and the orchestrator registers like any agent. Running it
as a managed pool of size 1 uses the same model and comes later.

## 3.7 Interfaces

- **REST** (FastAPI) under `/api` is the source of truth; its OpenAPI schema
  generates the dashboard client (`@argus/api-client`). `/healthz` stays at the
  root for probes; the dashboard is served at `/` by the same process.
- **MCP endpoint** for agents at `/mcp` (streamable HTTP, stateless, JSON
  responses), served by the same process. Agents connect with
  `Authorization: Bearer <token>`. Each tool is a thin in-process call to the
  REST API with the caller's own token, so scopes, validation, leases, and
  error codes are identical to HTTP; REST errors come back as tool errors
  (`409 stale_attempt: …`). The MCP layer holds no state or logic of its own.
  Tools: `claim_task`, `heartbeat`, `get_task`, `get_tasks`, `report_progress`,
  `complete_task`, `fail_task`, `ask_question`, `create_task`,
  `upload_text_artifact`, `list_artifacts`, `read_artifact`, `verify_artifact`,
  `leave`; `search_knowledge`, `get_knowledge_page`, `write_knowledge`;
  `lease_account`, `report_account_exhausted`; `list_engines`, `run_local`; and
  for orchestrators `get_inbox`, `list_tasks`, `unblock_task`, `retry_task`,
  `cancel_task`, `list_agents`, `list_pools`, `scale_pool`,
  `get_fleet_capabilities`, `launch_worker`, `list_worker_templates`,
  `create_worker` (see [worker launch](worker-launch.spec.md)), `route_task`,
  `list_decisions`, `get_decision`, `answer_decision`, `answer_decisions`. Lists
  return the page envelope ([MCP operations](mcp-operations.spec.md) 3.1).
  `tools/list` shows each token only the tools its scope can use
  ([MCP operations](mcp-operations.spec.md) 3.4). The server's instructions
  tell agents the working loop (claim, heartbeat, progress, complete; a 409
  means stop). The SDK's DNS-rebinding check is disabled because agents reach
  Argus by non-localhost names and every call needs a bearer token, which is what
  that check protects against.
- **SSE** stream (`GET /api/events/stream`) for the dashboard, fed by the
  in-process event bus and resumable with `Last-Event-ID` from the `events`
  table (replayed in batches until caught up). A subscriber that falls 1000
  events behind is disconnected rather than buffered without bound, and
  resumes on reconnect. `GET /api/events` pages history backwards.
- **Dashboard** (React, built static, served by Argus): task board, task detail
  with history and artifacts, agents, decision inbox, live event timeline. It
  authenticates with an admin token and follows the stream with `fetch` (not
  `EventSource`, which cannot send an `Authorization` header, so the token
  never goes into a URL).
- Every mutating call accepts an `Idempotency-Key` header; agents retry often.

## 3.8 NanoClaw runtime

*Built*: pool `runtime: nanoclaw` with `nanoclaw` options (`install_dir`,
`instances_root`, `agent_image`, `onecli_url`, `docker_socket`, `user`,
`group_add`); image and entrypoint in `deploy/nanoclaw-host`; details and
constraints in [`integrations/nanoclaw/README.md`](../../integrations/nanoclaw/README.md).

NanoClaw is a single Node.js **host** process that itself spawns one Docker
container per agent session, talking to it through per-session SQLite files
(`inbound.db` / `outbound.db`). It keeps credentials out of agent containers by
injecting them at the HTTPS layer (OneCLI Agent Vault or Iron Proxy), has a
pluggable provider interface (Claude Code by default), supports user-added MCP
servers, and is administered locally by the `ncl` CLI over a Unix socket (no
HTTP admin API). See https://docs.nanoclaw.dev/concepts/architecture.

For repository-backed tasks, NanoClaw is the execution/delivery plane. A task
completion should include worker-reported repository, branch, base/head SHA,
commit(s), PR number/URL/state, project-gate results, and CI/review status tied
to that exact head. These are observations reported to Argus, not independently
verified by the control plane. Non-code tasks need not carry PR metadata.

### Repository credentials and Gitea delivery

Repository access is separate from engine and executor accounts. A distinct
Argus `repository` account lease is requested only for tasks requiring the
`repository:write` capability. Pools configure a project-to-HTTPS
`owner/repository` allowlist; NanoClaw rejects an unconfigured project, host,
or repository before applying the lease. The account provider is `gitea`.
After the task, NanoClaw unassigns both OneCLI repository secrets and releases
the task-scoped `repository` lease. Tasks without this capability do not
acquire the credential and retain existing behavior.

Least privilege is preferred: use a dedicated Gitea bot/service identity with
permissions limited upstream to the configured repository when one is
available. For the accepted deployment, the operator cannot create a bot
account and has chosen the existing personal Gitea PAT for both REST and Git.
This personal-PAT fallback is permitted by that operator decision; it is not a
repo-scoped PAT and retains the user's Gitea account permissions. Do not imply
that proxy path scoping narrows those upstream permissions.

The operator registers the Gitea account through Argus's account registry
(provider `gitea`, kind `api_key`, credential fields `username` and `token`).
The username is required for Git smart-HTTP Basic authentication. The registry
validates both fields, writes the credential to the SOPS-backed secret store,
and keeps only the `sops:` reference in the database row. The dashboard's
Add account dialog offers `gitea` with username and token fields (the token is a
password input and is never shown again). Configure the
matching project URL in the NanoClaw pool's `nanoclaw.repositories` map.
The NanoClaw host writes two `generic` secrets to OneCLI 1.42, both scoped to the exact Gitea host:
the REST secret matches only `/api/v1/repos/{owner}/{repository}/*` and injects
`Authorization: token <PAT>`; the Git smart-HTTP secret matches only
`/{owner}/{repository}.git/*` and injects `Authorization: Basic <base64(username:PAT)>`.
The latter is created only in the trusted host-side lease mapper and is stored only in OneCLI's
encrypted secret store. Both are injected only through the OneCLI egress proxy, for the exact
configured Gitea host and allowlisted repository API/Git paths; the raw PAT never enters task data,
model prompts, agent environment/files, process arguments, logs or artifacts. Base64 is encoding,
not encryption. Path scoping is a compensating control at the trusted proxy boundary, not a
restriction on the personal PAT itself; residual risk remains if that proxy/admin boundary is
bypassed. The official API contract documents the
`token` scheme, while Gitea's Git-over-HTTP flow uses Basic with the service username and PAT as
password ([Gitea 1.26 API authentication](https://docs.gitea.com/api/1.26/)); the target
deployment version is not configured in this repository and must be confirmed during operator
provisioning.
Both patterns are repository-specific, and the worker's API helper separately enforces its exact
URL allowlist. Prefer the repo-limited bot identity when available; this deployment's personal PAT
is explicitly accepted only through SOPS and exact OneCLI host/path injection for the allowlisted
repository. Argus admin/API tokens and Hermes/model-provider credentials are forbidden for
repository access.
For repository identity, configuration may use either `/owner/repository` or
`/owner/repository.git`. Normalize exactly one terminal `.git` from the
repository component for REST API paths and allowlist comparisons; pass the
worker a canonical HTTPS clone URL ending in one `.git`. The REST rule uses
`/api/v1/repos/{owner}/{repository}/*`; the Git rule uses the exact normalized
remote prefix `/{owner}/{repository}.git/*`. Never derive either rule from the
un-normalized suffix or use a broad `repository*` match. Thus both configured
forms yield the same REST identity and Git remote, while lookalike repositories
remain rejected by exact normalized allowlist comparison.
OneCLI's documented generic-secret field is `injectionConfig`, and its path
matcher supports a single pattern with `*` wildcard forms ([OneCLI 1.42 secret
service](https://github.com/onecli/onecli/blob/v1.42.0/packages/api/src/services/secret-service.ts),
[path-matching contract](https://trybrowzer.com/docs/onecli/credential-injection/injection-modes-reference#path-matching)).
Do not use a `/**` suffix for Git paths: OneCLI 1.42 `path_matches` treats that as segment
wildcards and it does not match real smart-HTTP paths such as
`/{owner}/{repo}.git/info/refs`; injection silently skips and Git sees no `Authorization`.
Git also does not honor `SSL_CERT_FILE` / `NODE_EXTRA_CA_CERTS` for HTTPS verification.
The Argus NanoClaw install sets `GIT_SSL_CAINFO` to the same combined gateway CA bundle path
as `SSL_CERT_FILE` when mounted (otherwise the gateway-only CA path from OneCLI container
config), so `git clone`/`git fetch` through the proxy trust the MITM CA without disabling
verification. The Codex agent-runner rebuilds child process env from
`CODEX_ENV_ALLOWLIST` in `container/agent-runner`; the Argus install overlay must keep
`GIT_HTTP_PROXY_AUTHMETHOD=basic` and `GIT_TERMINAL_PROMPT=0` alongside the gateway
`GIT_SSL_CAINFO` and proxy variables so Git authenticates to the OneCLI SDK tunnel, plus
`CURSOR_CONFIG_DIR` and `XDG_CONFIG_HOME` so `argus-delegate` can materialize the gateway's
HTTP/1.1 Cursor stubs into a writable config dir, and `ARGUS_REPOSITORIES_JSON` so
`argus-gitea-api` sees the same project-to-URL allowlist as the NanoClaw host, while still
dropping raw provider secrets (API keys, tokens). The fleet sets `ARGUS_REPOSITORIES_JSON`
on the NanoClaw host container only; `install.sh` copies it into the agent container's
`contributedEnv` when composing a session. Inspecting `Docker` container `Config.Env` or
running `docker exec git` is not sufficient: those see the container entry env, not the
filtered env Codex passes to `tools.exec_command`; acceptance requires a `repository:write`
task whose engine performs a native HTTPS `git clone`/`fetch` with no `git config` CA or
proxy overrides. Repository credentials remain scoped to `…/repo.git/*`
only while the `repository` lease is held; release removes OneCLI assignments (a post-task
probe must not be mistaken for a live-task failure). Workers clone at runtime over HTTPS
via the injected proxy; project toolchain images do not bake source repositories.
It reconciles the complete currently-held engine/executor/repository lease set (including both
repository secrets)
because OneCLI's set-secrets endpoint replaces assignments, and the
agent-list endpoint does not report assignments.
Repository-write pools must also configure managed engine account providers so
the worker has a known base lease set; it fails closed rather than changing
OneCLI assignments when that set is unavailable.
The host retains OneCLI
admin access; the agent container has only the existing egress-proxy route,
never the OneCLI admin API or raw token. Git/REST use HTTPS through the proxy,
not SSH credentials. The worker API helper has no token parameter, verifies an
exact repository allowlist, supports PR create/read/update and CI status reads
tied to the PR head SHA, plus read-only `issue-get` (issue tracker item by
number) and `list-prs` (paginated PR list with explicit `--state` and capped
`--limit`). The `get` command is pull-request-only; a 404 on `get` does not
imply auth failure when the tracker number is an issue — use `issue-get`.
A retry of PR creation returns an existing open PR for the same source branch and
base rather than creating a duplicate. `updatePullRequest` does not accept a
merge flag; authorized delivery merges use the Gitea pull-request merge REST
endpoint through the same allowlisted OneCLI-proxied `GiteaApi.request` path as
other helper calls. The helper uses an Undici proxy dispatcher for Node fetch
(no direct-network fallback). The image overlay installs this helper and its
pinned Undici dependency. Configure the same project-to-repository allowlist in
the worker container as `ARGUS_REPOSITORIES_JSON`; without it the helper fails
closed.

The secret must not appear in task spec/result, model prompt, agent
environment/files, process arguments, logs, or artifacts. Argus receives only
non-secret delivery metadata. The injected task contract (`renderTask`) forbids
merge by default and states preconditions when a task sets
`input.delivery.allow_merge` to boolean `true` or when pinned project delivery
policy in knowledge authorizes it (spec prose alone is not a programmatic grant).

Consequences for Argus:

- **The unit Argus provisions is a NanoClaw host, not an agent container.**
  One Argus instance = one NanoClaw host container (with access to the Docker
  daemon) running exactly **one** agent group, which registers with Argus as one
  agent. **This is a decision, not a missing feature**: isolation is a goal, so
  agents never share a NanoClaw host process, working directory, vault identity
  or container namespace. `agents_per_instance` is therefore fixed at 1 for this
  runtime (the pool validation enforces it); more capacity means more instances.
- **The Docker socket is the remaining isolation gap, and sandboxing closes it.**
  With `docker.runtime` set (e.g. `sysbox-runc`) the host gets no socket and runs
  its own daemon, so each instance is isolated from the Docker host and from its
  siblings. A dedicated Docker host per pool (`docker.host`) adds a VM boundary on
  top and is the recommended hardening for untrusted workloads. Without
  `docker.runtime` the socket is mounted and root-equivalent on that host.
- **Argus ships a NanoClaw channel** (`integrations/nanoclaw/argus.ts`,
  TypeScript, installed into a NanoClaw checkout by `install.sh`; NanoClaw has no
  plugin loader). It is a native channel adapter in the host process, modeled on
  NanoClaw's CLI channel, and keeps the agent's Argus token host-side:
  1. Registers with its bootstrap token and heartbeats, renewing the lease on the
     task it holds.
  2. Long-polls `POST /api/tasks/claim` in code (no LLM loop burns tokens
     polling), one task at a time.
  3. Injects the claimed task (spec, acceptance, pinned knowledge bodies,
     reply instructions, repository delivery/merge rules when applicable, and
     the mandatory `argus-delegate` implementation contract from `renderTask`)
     as an inbound message, waking the agent container.
  4. Turns replies into Argus calls: a plain reply is a progress note;
     `COMPLETE:`/`FAIL:`/`ASK:` prefixes complete, fail, or ask. A 409 drops the
     reply and moves on.
  **Results of any length.** `result.summary` holds at most 10 000 characters. A
  `COMPLETE:` reply within that is sent as the summary. A longer one is never
  truncated silently or dropped: the channel uploads the **whole** text as the
  artifact `result-attempt-<n>.md` (UTF-8, `text/markdown`; existing artifact
  contract, bound to the attempt it holds), then completes with a bounded summary:
  the beginning of the response, a note giving the artifact id, size and SHA-256, and
  `result.artifacts = [id]`. A retried delivery reuses the artifact already uploaded
  for the attempt (same SHA-256; a different body under the same name gets a
  `-<sha8>` name), so it never duplicates one. Errors are classified: a lost lease
  (409) drops the reply; transient failures (network, 5xx, 429) are retried; a
  permanent rejection (other 4xx) fails the attempt with the reason instead of
  leaving the task claimed forever. **Attachments:** files the agent attaches to any
  reply while a task is held (NanoClaw's outbox) are uploaded the same way, under
  their own sanitized names, and listed in the completion's `result.artifacts`; one
  that cannot be stored is named in the summary instead of vanishing. Error text is always the server's real message
  (validation errors included), never `[object Object]`.
- **MCP is the alternative reply path**: the Argus MCP endpoint (3.7) can be added
  to an agent group as a user MCP server for richer tools (knowledge, accounts),
  at the cost of giving the agent container an Argus token, which the channel's
  text protocol avoids.
- **Credentials never enter agent containers.** Argus is the source of truth
  for credentials (3.9). When a session leases an account, the module loads
  that credential into NanoClaw's gateway, which injects it at the HTTPS layer;
  the gateway is a sink, not a second store. Raw secrets never travel through
  task payloads.
  An `anthropic` credential document is read as `api_key`, `oauth_token`, or Claude
  Code's own `claudeAiOauth.accessToken`; an access token already past its
  `expiresAt` is refused (the channel does not refresh OAuth tokens), never applied.

## 3.9 Account pool and fallback

*Built for NanoClaw* (`integrations/nanoclaw/argus-accounts.ts`): the channel
leases an account, writes its credential into the OneCLI vault as one secret per
agent group (selective mode), reports exhaustion when the agent's failure notice
carries the provider's `Rate limit`/`Out of credits` message, switches the secret's
value to the next account, and re-sends the task. Details and limits in
`integrations/nanoclaw/README.md`.

Agents run on subscription or API accounts (Claude, Codex, Cursor, …) that hit
usage limits. Argus owns the **account registry and leasing** (in SQLite) and
the **credentials** (in a SOPS file, 3.9.1). The two are kept apart: the
database never holds a secret.

- **`accounts`**: `id`, `name`, `provider` (`anthropic` | `openai` | `cursor` |
  …), `kind` (`api_key` | `subscription`), `secret_ref` (`sops:<name>`, a key
  into the secrets file; never the secret itself), `priority`, `max_leases`
  (concurrent leases allowed; empty = unlimited), `state` (`available` |
  `exhausted` | `disabled`), `exhausted_until`, `disabled_reason`, `labels`.
  Admins register accounts (`POST /api/accounts`) either with the credential,
  which Argus writes to the SOPS file as `account-<id>`, or with a `secret_ref`
  to an entry already there.
- **Edit, replace credential, delete** (admin). `PATCH /api/accounts/{id}` changes
  `name` (unique; `409 name_taken`), `priority`, `max_leases`, `labels` and
  `enabled`; it never touches the credential and never returns it. The credential
  is replaced only by `PUT /api/accounts/{id}/credential` (or the Claude renewal
  login). The dashboard's **Replace credential** (every provider except
  `anthropic`, which has Renew credential) sends a blank-by-default JSON document,
  pasted or read from a file the operator picks; stored values are never loaded
  into the form. The server validates it with the provider's own check where one
  exists (`gitea`; `openai`, `anthropic` as for registration) and otherwise
  requires a non-empty JSON object. Replacing changes nothing but the secret: a
  disabled account stays disabled. A document is only stored, never treated as
  proof of authentication.
  Disabling is reversible and is not deletion.
  `DELETE /api/accounts/{id}?confirm_name=<name>` removes the registry row, its
  usage history and its ended leases, and the `account-*` secret Argus generated
  unless another account still references it (operator-managed refs are never
  removed). `confirm_name` must equal the current name (`409 name_mismatch`:
  a renamed or wrong target is refused) and an account with an active lease is
  refused (`409 account_in_use`); disabled accounts can be deleted.
  **The SOPS file and SQLite do not share a transaction.** The secret is removed
  inside the SQL transaction, so a store failure rolls the rows back; if the SQL
  commit then fails after the secret was removed, the route writes the exact prior
  value back (best effort compensation, not atomicity). If that restore also
  fails the error (`secret_restore_failed`) names the entry, never a value, and the
  operator recovers it from the secrets file backup. An `account.deleted` event
  records name and provider, never a credential. The dashboard asks the operator to
  type the account's name.
- **Leasing** (`POST /api/accounts/lease {providers}`, most preferred first):
  Argus picks the highest-priority usable account among those providers (the
  agent's provider order breaks ties, then the least-leased account) and returns
  it **with its credential**. That response is how a plain-runtime worker gets
  the secret; the NanoClaw module instead loads it into the gateway (3.8). An
  agent holds at most one lease per role (`engine`, the default: the account its
  harness runs on; `executor`: the account a CLI it delegates to uses, see
  [codex accounts](codex-accounts.spec.md)); leasing again replaces the lease of
  that role, and leases end when the agent is lost or leaves (or on
  `POST /api/accounts/release`, optionally for one `role`).
- **Fallback**: on a quota or rate-limit error the leasing agent reports
  `POST /api/accounts/{id}/exhausted {until?, reason, task_id?, attempt?}`.
  Argus marks it exhausted until the reported reset time (or
  `ARGUS_ACCOUNT_COOLDOWN_MINUTES`, default 60) and immediately leases the next
  account, **same provider first**, then the agent's other providers. Accounts
  return to `available` on their own when the reset time passes (reaper).
- **No accounts left**: the response carries no lease and the earliest reset
  time; the given task is blocked with reason `no_account`. Such tasks return to
  `ready` as soon as any account becomes usable again (recovery, a new account,
  re-enabling, or a secrets reload), and whoever claims them leases afresh.
- **Usage is recorded, not enforced**: `result.usage.account_id` on completion,
  plus interim `POST /api/accounts/{id}/usage`. Accounts list usage for the last
  24 h and in total.

Check each provider's terms before pooling subscription accounts for automated
use; some consumer plans restrict it.

### 3.9.1 Credential store: SOPS + age

- Credentials live in one SOPS-encrypted YAML file (`ARGUS_SECRETS_FILE`,
  default `/data/secrets.sops.yaml`), encrypted to one or more **age**
  recipients. Each top-level key is a secret name holding that account's
  material (an API key, or a subscription's OAuth tokens).
- Argus decrypts the file into memory at startup with the age identity sops
  finds (`SOPS_AGE_KEY_FILE` or `SOPS_AGE_KEY`). Plaintext is never written to
  disk, logged, or returned by the API except in a lease response to the agent
  holding the lease.
- **Argus is the only writer.** Each change runs `sops set --value-stdin` (or
  `unset`) on a temporary copy in the same directory, which is fsynced and
  renamed over the original; values reach sops over stdin, never on the command
  line. The first write creates the file, which needs `SOPS_AGE_RECIPIENTS`
  (the public key(s)); without it, writes fail with
  `secret_store_not_configured`.
- **Credential updates** (`PUT /api/accounts/{id}/credential`, e.g. refreshed
  OAuth tokens) are allowed for the agent holding a lease on that account, or an
  admin. If the write fails, the account is **disabled** with the reason, rather
  than keeping a token that would be lost on restart.
- Editing the file by hand (`sops /data/secrets.sops.yaml`) is supported;
  `POST /api/admin/secrets/reload` re-reads it.
- At startup and on reload, accounts whose secret is missing are disabled
  automatically, and re-enabled automatically once it is back. Accounts an admin
  disabled stay disabled. A store that cannot be decrypted is logged, never
  fatal.
- Because the file is encrypted, it can be backed up anywhere or committed to a
  private config repository. Losing the age identity means losing the
  credentials; the operator keeps a second recipient key offline.

## 3.10 Decision requests (human in the loop)

One primitive covers every "someone must decide before this continues" case,
instead of a special state per case. A **decision** has `subject` (task, pool,
agent), `question`, `options` (or free text), `requested_by`, `audience`
(`human` | `orchestrator` | `any`), `state` (`open` | `answered` |
`cancelled`), `answer`, `decided_by`.

Who may answer: `human` → admin tokens; `orchestrator` and `any` →
orchestrator or admin tokens. Workers never answer decisions.

Uses:

- **Questions from a worker** (`POST /tasks/{id}/ask`, lease required): the
  task goes `blocked` (lease released) until answered. The answer is appended to
  the task spec under a **Clarifications** heading and the task returns to
  `ready`; the next claim sees it. Workers stay pull-only.
- **Start gate**: a `requires_approval` task opens an `approval` decision
  (audience `human`, options `approve`/`reject`); reject cancels the task.
- **Review gate**: completion of a `review` task opens a `review` decision
  (audience follows `review`, options `accept`/`reject`, `text` as notes).
- **Policy gates**: an agent that has created `ARGUS_MAX_TASKS_PER_AGENT_HOUR`
  (default 100; 0 disables) tasks in the last hour gets further tasks created
  in `awaiting_approval` with a `policy` decision for a human, instead of a
  refusal. The scaling-threshold gate arrives with pools (3.5).
- **Moot decisions**: answering closes the decision before its transition
  runs; any other move out of the state a decision waits on (or into a terminal
  state) cancels it, e.g. a task cancelled while its question is open.

`GET /decisions` is the inbox (open, oldest first); `POST
/decisions/{id}/answer` takes `{choice, text}`. The dashboard renders the
inbox. Chat notifications are a later, optional integration (single-node 3.5).

## 3.11 Task kinds and result envelope

- Every task has `spec` (Markdown the agent reads) and `acceptance` (a list of
  checkable criteria, which makes review gates concrete).
- Every task has a `kind`. The **`task_kinds`** registry can attach a JSON
  Schema for `input` and for `result.data`, default `required_capabilities`,
  default lease, and default review policy. A kind without schemas is
  free-form, so ad-hoc work never waits on a schema being written. Unknown kinds
  are rejected; `generic` exists by default.
- Kind defaults fill only fields the creator left unset; explicit values win.
  Kinds are managed with `PUT /task-kinds/{name}` by orchestrators and admins;
  schemas are checked as JSON Schema (draft 2020-12) when saved.
- Every result uses one envelope, validated server-side:
  `{summary: str, data: object | null, artifacts: [artifact id], usage: Usage}`.
  `data` is validated against the kind's schema when one exists, and every
  listed artifact must have been uploaded for this task. A failing input or
  result is rejected with `422 validation_failed` and a `details` list naming
  each violation, so the agent can fix it and retry.

## 3.12 Progress, logs, and artifacts

| Output | Example | Home |
| --- | --- | --- |
| Progress notes | "tests written, fixing 2 failures" | `POST /tasks/{id}/progress` with a message and optional percent. Latest note on the task row, history in `events` (`task.progress`), shown live on the dashboard. A progress post is a worker write (3.3) and also renews the lease. Rate-limited per task (one per 10 s, `429` otherwise). |
| Container logs | agent stdout/stderr | Not stored. The dashboard streams them live from the target (`Target.logs`) while the container exists. |
| Artifacts | files, diffs, reports, transcripts | `POST /tasks/{id}/artifacts?attempt=&name=` with the raw body, by the lease holder, before completing; listed by id in `result.artifacts`. Stored under `/data/artifacts/tasks/<task_id>/<attempt>/<name>`, immutable, with size (`ARGUS_ARTIFACT_MAX_BYTES`, default 100 MB) and SHA-256 recorded. Bytes are staged to a temporary file outside the write transaction; a short transaction then checks the lease, records the row, and renames the file into place (single-node 3.4). |

# 4. Open Questions

1. **NanoClaw** ([research](../research/2026-10-14.nanoclaw-integration.research.md)):
   an Argus channel is feasible (messages can be injected without a chat
   platform); credential switching is per agent group through OneCLI 1.42.0 and
   unverified on a running session; a NanoClaw host needs the Docker CLI and
   identical host/container paths for its mounts, and there is no Kubernetes
   driver, so the `nanoclaw` runtime is Docker-target only for now.
2. **Exhaustion signals per provider**: which errors and headers reliably mean
   "quota exhausted until T" for Claude subscriptions, Codex, and Cursor?

# 5. Constraints

- Everything in [single-node](single-node.spec.md) applies: one process, SQLite
  with one writer, no required external services.
- No network calls inside a write transaction. Provider, target, and gateway
  calls happen after commit and are retried idempotently.
- Workers can crash, hang, or lose the network at any point. Every rule above
  must hold under those conditions; correctness comes from leases and fencing,
  never from trusting an agent to report its own death.
- Agents are LLMs: their calls may repeat, arrive out of order, or carry wrong
  ids. The API validates every transition server-side and returns clear,
  actionable errors.
- A runaway orchestrator must not be able to exhaust the host (`max_replicas`,
  human-only cap changes, policy gates).

# 6. Implementation Consequences

Build order, each step shippable and testable on its own:

1. **Data layer.** SQLite repository with one writer and a reader pool, SQL
   migrations via `user_version`, startup lock file, optional Litestream restore.
2. **Task API.** Create/list/get, the 3.2 state machine as one pure function
   with exhaustive tests, claim, atomic ownership and deadline checks from 3.3,
   events, in-process bus with long-poll. Cover writes and renewal after expiry
   but before reaping, stale attempts after reassignment, and expiry while
   waiting for the writer.
3. **Agents + reaper.** Register, batched heartbeat renewal, independent task
   lease expiry, irreversible `lost` handling and immediate token revocation.
4. **Auth scopes** (3.6).
5. **Task kinds, result envelope, progress, artifacts** (3.11, 3.12).
6. **Decisions** (3.10), including start and review gates.
7. **Dashboard v1**, served by the API: task board, agent list, decision inbox,
   event timeline over SSE.
8. **Account pool** (3.9) and the SOPS credential store (3.9.1).
9. **Pools + reconciler + `docker` target + `plain` runtime**, with an
   integration test that scales a pool of a trivial worker image up and down.
   Cover agent loss while its container stays running, unreachable providers,
   and Argus restart during termination or an ambiguous start; unresolved
   instances retain capacity and replacements wait for confirmed termination.
10. **MCP endpoint** over the same operations.
11. **`nanoclaw` runtime + Argus module** (3.8), after a spike on 4.1.
12. **`kubernetes` target.**

`argus_core` holds the state enums, transition table, and Pydantic models shared
by the API, the MCP layer, and any Python worker SDK.
