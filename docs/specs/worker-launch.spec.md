# Worker launch and node capabilities

# 1. Purpose and Scope

Let a human (dashboard) or an orchestrator (MCP) **request one more worker** and
watch it come up, and let Argus report what *this node can actually run*
instead of promising it. Builds on pools, the reconciler, and the runtimes in
[worker lifecycle 3.5 and 3.8](worker-lifecycle.spec.md).

In scope:

- `GET /api/fleet/capabilities`: probes each target the node uses (the default
  local Docker daemon plus every target a pool names) and judges each pool
  against it.
- `POST /api/pools/{name}/launch`: asks for one more instance of a pool.
- MCP tools `get_fleet_capabilities` and `launch_worker`: thin calls to the two
  routes above, as for every other tool (worker lifecycle 3.7).
- Dashboard, Agents view: a **Create worker** panel (pool picker, readiness,
  blockers, launch button) and a **Launches** table that follows each pool's
  instances from `reserved` to `running` or to its error.

Guided onboarding (added): the ordinary way to get a first worker is a
**template**, not the raw pool form. `GET /api/fleet/templates` lists the worker
templates Argus supports, which nodes can run each, and exactly what is missing
where one cannot. `POST /api/fleet/workers` creates the pool from a template and
requests its first workers in one confirmed call, so a fresh install with no
pools is not stranded. The dashboard form (Agents view) and the MCP tools
`list_worker_templates` / `create_worker` use the same two routes.

# 2. Non-Goals

- Replacing the low-level pool API. `PUT /pools/{name}` and the pool editor stay
  for anything a template does not cover (Kubernetes, custom images, remote Docker
  hosts, bespoke NanoClaw options); templates only cover the common path.
- A registry or image catalogue service. Templates are a small explicit list in
  the backend plus labels on images already installed on the node.
- Kubernetes in the guided flow (use the pool editor).
- Launching outside a pool, or any new provisioning path. The reconciler remains
  the only thing that starts containers.
- Raising `max_replicas`. Launch never exceeds the cap; asking for more than the
  cap is `scale_pool`, which opens a human decision for orchestrators.
- Launching orchestrators (they start outside Argus, worker lifecycle 3.6).

# 3. Decisions Made

- **A launch is a desired-state change**, not a spawn: it adds one to
  `desired_replicas` in one write transaction and records `pool.launch_requested`.
  The reconciler reserves the slot and starts the container within one interval.
  The response says *requested*; success is only an instance reaching `running`
  (and later an agent registering). Failures surface on the instance
  (`last_error`, `start_attempts`) and on the pool (`status`, `last_error`).
- **Preflight, no false success.** Before changing anything, launch probes the
  pool's target (outside any write transaction) and refuses with `409
  not_launchable` and the blocking reasons when the pool cannot start an
  instance now. A refused launch changes nothing.
- **Scopes.** `admin` and `orchestrator` may read capabilities and launch;
  `worker` gets `403`. Launch is bounded by `max_replicas` for both (`409
  pool_at_capacity`); unlike `scale`, an admin does not raise the cap implicitly.
- **No duplicate submissions: a launch needs a precondition.** Exactly one of
  these is required:
  - `expected_desired_replicas`: a compare-and-set. The launch applies only if the
    pool's `desired_replicas` still equals it, else `409 stale_pool`.
  - `unconditional: true`: an explicit opt-in to no compare-and-set, always adds
    one. For callers that accept the duplicate risk; a retry of it can add another
    worker.

  Neither is `422 precondition_required`; both is `422 precondition_conflict`.
  Both are refused before the pool is probed or anything is written. The dashboard
  always sends `expected_desired_replicas` and disables the button while a request
  is in flight, so a double click or two tabs create one worker. Orchestrators
  (MCP) must pass it too.
- **Acknowledgment and retries.** `200` means the desired-state write committed
  exactly once. If the response is lost, the caller repeats the same request with
  the same `expected_desired_replicas`: it now gets `409 stale_pool` and creates
  nothing. (It cannot tell its own committed launch from someone else's change by
  the code alone; it reads the pool to see `desired_replicas`.) `desired_replicas`
  is in the database, so this holds across a process restart: a retry after a
  restart sees the committed value.
- **Non-repeatable effects.** Launch itself starts nothing; it only changes
  `desired_replicas`, so repeating it cannot start a container. A container is
  started only by the reconciler, from desired versus existing instances. It
  commits an `instance` row in state `reserved` (with its container name derived
  from the instance id, and a worker display name — see below) before calling
  the target, and one pass sees at most one start per reserved instance. After a
  crash between reserve and start, the next pass observes the pool's containers:
  a `reserved` instance whose container is running is marked `running`, one that
  exited is terminated, and one with no container is started again under the
  same name (a name conflict from the daemon or Kubernetes is treated as
  "already created", not an error). A failed or ambiguous start leaves it
  `reserved` with `last_error` and `start_attempts` visible and is retried with
  backoff. This is reconciliation by deterministic name, not a transactional
  guarantee between the database and the target.
- **Worker display names.** Each reserved instance gets a free single-word
  display name from the same mythology/constellation list the catalog uses for
  pool suggestions (`instances.agent_name`). The reconciler seeds the pick from
  live agent names, other non-terminated instances' assigned names, and pool
  names, so two workers never share a word and a worker is never named after a
  pool. The name is stored on the instance row (stable across start retries) and
  passed to NanoClaw as `ARGUS_AGENT_NAME`, which becomes the Argus agent name
  and the OneCLI agent name. Rows that predate the column keep the legacy
  `<pool>-<id[:8]>` form for cleanup matching. Pools are usually one per install;
  the worker name is what operators see on chips and in the rail.
- **Discovery is measured, never configured.** A check is `ok: true`,
  `ok: false`, or `ok: null` (could not be verified, with why); only `false`
  blocks. Docker targets report reachability, engine version, OCI runtimes,
  CPUs and memory; a pool is checked for: public URL set, target reachable,
  `docker.runtime` present on the daemon, requested CPU/memory within the node,
  image (and NanoClaw agent image) present on the daemon, free slot under the
  cap, that a NanoClaw pool with `cpus` and/or `memory` set gives its sessions
  a quota (`session_quota`: `ok: true` when Argus sets `CONTAINER_CPU_LIMIT`
  and/or `CONTAINER_MEMORY_LIMIT` from the pool request on the host env, which
  NanoClaw's `composeSessionSpec` maps to `SessionSpec.resources` and the
  Docker driver realizes as `--cpus`/`--memory`; omitted when the pool is not
  NanoClaw or requests no quota). This check is static composition evidence,
  not a live `docker inspect` of a spawned session — that remains the
  [session quota smoke](../runbooks/2026-10-30.session-quota-smoke.ref.md)
  runbook. And, for NanoClaw on the local daemon only, its host paths.
  Anything about a remote daemon's filesystem is `null`, not guessed. An image
  that is absent but pullable is `null` ("will be pulled on start"), since a
  registry is not probed.
- Probes time out after 5 s per target and are read-only.
- **Templates are authoritative, small, explicit backend definitions**
  (`fleet/templates.py`), one per supported integration:
  - **Two NanoClaw templates, with different isolation, never merged or
    substituted.** Both run a NanoClaw host (3.8), one agent per instance, and
    share `ARGUS_NANOCLAW_INSTALL_DIR`, `ARGUS_NANOCLAW_INSTANCES_ROOT`,
    `ARGUS_NANOCLAW_AGENT_IMAGE`, `ARGUS_NANOCLAW_HOST_IMAGE` (default tag
    `argus-nanoclaw-host:dev`, what `deploy/nanoclaw-host` documents building).
    The built checkout must register a gateway provider (its `dist/gateway-providers/
    installed.js` imports the OneCLI adapter, which `provision.sh` installs): without
    it the host exits at startup, so neither template is ready. Readiness covers
    *bootstrap* only; the gateway **service** (upstream's dependency, not Argus's) is
    needed later, when an agent session starts, and is not probed.
    Each judges *its own* prerequisites independently, so a missing sandbox runtime
    never blocks the socket template, and artifacts without the opt-in never make
    the socket template ready:
    - `nanoclaw-sandbox` ("NanoClaw, sandboxed"): the host runs under the OCI
      runtime `ARGUS_NANOCLAW_DOCKER_RUNTIME` (e.g. `sysbox-runc`, which the
      daemon must offer) with its own inner daemon and loads
      `ARGUS_NANOCLAW_AGENT_IMAGE_ARCHIVE`. No Docker socket is mounted.
    - `nanoclaw-socket` ("NanoClaw, host Docker socket"): the host gets the
      node's Docker socket, which is **root-equivalent on that Docker host**.
      Offered only when the operator opts in with
      `ARGUS_NANOCLAW_ALLOW_DOCKER_SOCKET=true` (a process-level policy the
      request cannot change). The same opt-in is enforced on every
      `PUT`/`PATCH /api/pools/{name}` that would mount the socket, so the raw
      pool editor cannot bypass the template gate. Needs the agent image on the
      node's daemon, the socket (`ARGUS_NANOCLAW_DOCKER_SOCKET`, default
      `/var/run/docker.sock`), and `ARGUS_NANOCLAW_USER` (`uid:gid`) /
      `ARGUS_NANOCLAW_GROUP_ADD` (the socket's group id) so the unprivileged host
      process can use it. Readiness measures, on a daemon sharing this
      filesystem, that the socket exists, that the configured user can reach it
      (root, group in `GROUP_ADD`, or world access), and that the install dir is
      readable by that user. `integrations/nanoclaw/provision.sh env` derives
      these ids from the host instead of hard-coding them.
    The id `nanoclaw` (before the split) is **not accepted** for creation
    (`unknown_template`): it did not say which isolation it meant, and picking one
    would silently downgrade or upgrade it. Existing pools keep whatever they were
    stored with; nothing rewrites them. The security note of each template is part
    of the catalog (REST, MCP, and shown in the dashboard before submit).
  - `argus-worker`: an image that speaks the Argus API (`plain` runtime). Not
    every image qualifies. An image is offered only if it carries the label
    `argus.worker=plain` on the node's daemon (optionally
    `argus.worker.capabilities=a,b`, its capabilities) or is listed by the
    operator in `ARGUS_WORKER_IMAGES` and is present on the node. The reference
    `examples/echo-worker` image carries the label.
- **Nodes.** `local` (this Argus process's Docker) is preselected; other nodes
  are only Docker hosts already configured (`ARGUS_FLEET_DOCKER_HOSTS` or used by
  an existing pool), never a host typed into a request. Each is probed.
- **Readiness is derived, never claimed.** A template is `ready` on a node only
  if every prerequisite is verified true; false *and* unverifiable prerequisites
  both make it not ready and are listed under `missing` with how to fix them.
  Host paths (NanoClaw install dir, instances root, archive) are verified only
  when the Docker daemon provably shares this process's filesystem (local unix
  socket, not Docker Desktop); on a remote or VM daemon they are unverifiable,
  so the guided flow refuses and points at the pool editor. (Launching an
  *existing*, admin-defined pool still treats unverifiable checks as non-blocking
  and shows them as unverified.)
- **Create is server-authoritative.** The request names a template, node,
  **required `name`** (the pool name), quantity (default 1) and, for `argus-worker`, an image chosen
  from the offered ones. The server rebuilds the whole pool from the template and
  the node's facts; the client never supplies image, runtime, or NanoClaw options,
  and an unknown template or image is rejected (`unknown_template`,
  `unknown_image`). Defaults (quantity 1, cap `max(quantity, 3)`, extra
  host `host.docker.internal:host-gateway`, template capabilities) come from the
  catalog and are the same for UI and MCP. The catalog also *suggests* a free
  pool name, which the dashboard fills in (editable); the server never picks one
  itself. The suggestion uses the same mythology/constellation word list as
  worker display names (about 170 entries: `atlas`, `nyx`, `lyra`, `orion`,
  ...), the same for every template. It is deterministic for a given set of
  existing pool names (seeded from that set, no clock or RNG state), so
  re-reading the catalog does not reshuffle it, and it changes only once the
  suggested name is taken. It skips names already used by a pool and falls
  back to `<word>-2`, `<word>-3`, ... only when the whole list is taken. The
  list is a static module in `argus_api.fleet`, English only, with no
  dependency, and every entry matches the pool name pattern. Worker names are
  assigned separately at instance reserve (see above); pools are usually one.
- **Selecting or discovering never creates anything.** Only the confirmed POST
  writes: one transaction creates the pool (`desired_replicas` = quantity) and
  records `pool.created`. The name is the idempotency key: it is required (omitting it is `422`), so a
  retry repeats the same name, and a name that exists is `409 pool_exists`. A
  retry or a concurrent duplicate therefore creates no second pool and no extra
  worker. (An auto-picked name would turn every retry into `worker-2`, `worker-3`,
  which is why there is none; no separate key store is needed.) adding a worker
  to an existing pool is `launch`. Refusals (not ready, unknown template, bad
  input) happen before the write and leave no pool. Success still only means
  *requested*; the instance and pool status track the real start.
- **Scopes for provisioning.** `admin`: everything above plus `advanced`
  overrides (capabilities, environment, extra hosts, `max_replicas`). An explicit
  cap must be at least the quantity (`422 invalid_cap`) and at most 1000; unset,
  it is `max(quantity, 3)` and the dashboard shows that default. `orchestrator`: the catalog
  and `create_worker` with template defaults only (`advanced` is `403`), quantity
  within the default cap. `worker`: `403` on both routes.
- **Guided form layout and choice policy** (dashboard):
  - Template choices are one native radio group in a fieldset; each choice is a
    full-width row: radio directly beside the name and status, description below.
  - **Nothing is preselected unless it is both safe and ready**: the first ready
    template without a `warning`. A template with a warning (the Docker-socket one)
    is never preselected, and a not-ready template is never the default. With no
    safe ready option no template is chosen, a short prompt says so, and Create is
    disabled. Selecting a not-ready template to read why is allowed; Create stays
    blocked.
  - The chosen template shows one concise blocking reason; the raw prerequisite
    list, setup instructions and `ARGUS_*` names live in a collapsed "Setup
    details". The security `warning` and its acceptance checkbox are always visible
    before Create (never inside a disclosure), and the notes, including that task
    execution needs the gateway service and credentials, which the catalog does not
    verify, stay visible. Advanced is collapsed initially.
- A template fixes its isolation: `nanoclaw-sandbox` always sets `docker.runtime`
  and never mounts the socket; `nanoclaw-socket` never sets `docker.runtime`.
  There is no request field, fallback, or global mode that changes it.
- Pools record the `template` they came from (a field in the pool's config JSON,
  no migration). The pool editor preserves every field it does not show,
  including `docker.runtime`, `docker.network`, and `nanoclaw`.

# 4. Open Questions

- Should a launch for a pool at its cap let an orchestrator open a decision
  like `scale_pool` does? Today it is refused; `scale_pool` is the path.
- Crash between reserve and start: the reconciler's behaviour above is read from
  the code and covered only by the fake target. Is a crash after the container is
  created but before the instance is marked `running` (or a target that creates
  and then errors) proven never to yield two containers for one instance, on both
  Docker and Kubernetes? Untested against a real daemon.
- Should a lost-response retry be distinguishable from a foreign change (e.g. an
  idempotency key echoed in the result), instead of reading `desired_replicas`
  after `stale_pool`?
- Registry reachability and image pullability are not probed, so a template image
  must already be on the node.
- Should an orchestrator be allowed to create a *new* pool (from a template only)?
  Taken as yes, bounded as above; confirm before relying on it.
- Kubernetes and remote-host NanoClaw templates need a way to verify the remote
  filesystem; until then they are pool-editor only.

# 5. Constraints

- Single-node (single-node spec): no new service, table, or migration; state is
  `pools` and `instances`.
- All writes through `Database.write()`, time from `tx.now`; the target is
  never called inside a write transaction.
- MCP holds no logic: it forwards to REST with the caller's token.

# 6. Implementation Consequences

- `argus_core.fleet`: `Check`, `TargetCapability`, `PoolCapability`,
  `FleetCapabilities`, `LaunchRequest`, `LaunchResult`.
- `fleet/targets.py`: `Target.probe()` and `Target.image_present()`.
- `fleet/capabilities.py` (discovery), `fleet/service.launch`, two routes in
  `fleet/routes.py`, two tools in `mcp_server.py`, OpenAPI and client regenerated.
- `fleet/runtimes.session_quota_env` / `container_spec`: a NanoClaw host whose
  pool sets `cpus` / `memory_mb` receives `CONTAINER_CPU_LIMIT` /
  `CONTAINER_MEMORY_LIMIT` so agent sessions inherit the same quota.
- `fleet/capabilities.session_quota_check`: the `session_quota` readiness check
  for NanoClaw pools that request a quota.
- `fleet/templates.py`, `POST /fleet/workers`, `GET /fleet/templates`, MCP
  `list_worker_templates` / `create_worker`; `Target.probe` reports whether the
  daemon shares this filesystem; `Target.labeled_images`; `PoolUpsert.template`.
- Dashboard: `views/Agents.tsx` (guided form, advanced foldout) and `Pools.tsx`
  (editor preserves hidden settings).
- Tests: scopes, discovery available/unavailable, `session_quota` evidence,
  launch success and each refusal, MCP, and the view's pure state logic.
- Live Docker confirmation of session `--cpus`/`--memory` and OOM classification
  signals: [session quota smoke](../runbooks/2026-10-30.session-quota-smoke.ref.md).
