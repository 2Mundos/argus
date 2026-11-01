# Operations Spec

Health, readiness, metrics, and the System widgets. Everything here follows
[lightweight single-node](single-node.spec.md): no required external service, and
nothing to install to know whether Argus is healthy.

# 1. Purpose and Scope

An operator (or an orchestrator, a probe, a Prometheus) must be able to answer,
without reading code or logs:

- Is the process alive, and is it *able to do its job* (not merely running)?
- Is work flowing: are tasks waiting, are agents alive, are pools converged?
- Is something silently stuck: a wedged database writer, a loop that stopped, a
  disk that filled, an unreachable provider, a pool that cannot start?

Surfaces: `GET /healthz` (liveness), `GET /readyz` (readiness), `GET /api/metrics`
(Prometheus text), `GET /api/system` (the same facts as JSON for the dashboard),
and the dashboard's Home.

# 2. Non-Goals

- A metrics store, dashboards, alerting rules, or log aggregation. Argus exposes
  facts; storing and alerting on them is the operator's stack (single-node 2).
- Distributed tracing or per-request latency histograms in v1.
- Business-level SLOs. The metrics make queue age and failure rates visible; what
  counts as healthy for a fleet is the operator's call.

# 3. Decisions Made

## 3.1 Liveness and readiness are different questions

- `GET /healthz`: cheap, unauthenticated, reads the database once. "The process
  is up." Unchanged.
- `GET /readyz`: unauthenticated, `200` or `503`, body lists each check as
  `ok` | `degraded` | `failing`. It answers "can Argus do its job":

  | Check | Failing when | Notes |
  | --- | --- | --- |
  | `database` | a read snapshot does not complete within 5 s, or one write transaction has held the writer for over 5 s | A wedged single writer is the failure that matters. The probe never acquires the writer (3.1a). |
  | `reaper` | no pass completed within `max(3 × interval, 30 s)` | Leases would stop expiring. |
  | `reconciler` | same | Pools would stop converging. |
  | `disk` | free space on the data directory below 64 MiB | SQLite cannot write. |
  | `secrets` | *degraded*, never failing, when the secret store failed to load | Optional by design (single-node 3.5). |

  Readiness is `503` only when a check is `failing`; `degraded` still returns
  `200`. The body carries no secrets and no identifiers.

### 3.1a `/readyz` is anonymous, so it must be cheap and cannot take the writer

- Readiness never calls `Database.write()`: an unauthenticated client must not be
  able to queue on, or hold, SQLite's single writer. The `database` check runs a
  trivial query on a read connection under a 5 s timeout, and asks
  `Database.write_held_for()` how long the current write transaction has held the
  writer (a monotonic timestamp read, no lock). Held over 5 s means `failing`.
- The result is cached per app for 2 s and concurrent callers share one in-flight
  probe, so request rate does not change probe cost. `/api/system` reuses it.
- Detail strings carry no identifiers.
- Exposure: the image binds `UVICORN_HOST` (default `0.0.0.0` inside the
  container, which published ports require). Who can connect is set by the publish
  address, so the documented `docker run` publishes on `127.0.0.1`. Argus speaks
  plain HTTP; TLS terminates at a reverse proxy, none of which ships in this repo.

## 3.2 Metrics

`GET /api/metrics` returns the Prometheus text exposition format, hand-rendered (no
client library at runtime) and computed at scrape time from the database with a
handful of aggregate queries. It requires an **admin token** (Prometheus supports
`authorization: credentials`); create one labeled `prometheus` with `POST
/api/admin/tokens`.

All names are prefixed `argus_`. Gauges unless noted.

| Metric | Labels | Meaning |
| --- | --- | --- |
| `argus_build_info` | `version` | always 1 |
| `argus_uptime_seconds` | | process uptime |
| `argus_tasks` | `state` | tasks per state; every state present (0 when none) |
| `argus_task_oldest_ready_age_seconds` | | age of the oldest `ready` task, 0 if none: the queue-latency signal |
| `argus_agents` | `state` | agents per state |
| `argus_instances` | `pool`, `state` | instances per pool and state (not terminated, plus terminated) |
| `argus_pool_replicas` | `pool`, `kind` | `kind` is `desired`, `max`, or `live` |
| `argus_pool_status` | `pool`, `status` | 1 for the pool's current status (`ok`/`unreachable`/`failing`) |
| `argus_accounts` | `provider`, `state` | accounts per provider and state |
| `argus_account_tokens_total` (counter) | `provider`, `account`, `kind` | reported tokens: input, output, cached ([account usage](account-usage.spec.md)) |
| `argus_account_quota_used_percent` | `provider`, `account`, `window` | latest provider-reported % used per quota window |
| `argus_decisions_open` | | decisions waiting for an answer |
| `argus_events_total` | | counter: rows ever written to the event log |
| `argus_loop_last_success_timestamp_seconds` | `loop` | unix time of the loop's last good pass |
| `argus_loop_duration_seconds` | `loop` | duration of the last pass |
| `argus_loop_errors_total` | `loop` | counter of failed passes |
| `argus_secret_store_up` | | 1 if the secret store loaded |
| `argus_database_size_bytes` | `file` | `main` and `wal` |
| `argus_disk_free_bytes` | | free space on the data directory's filesystem |

Label values come from Argus's own enumerations and pool names (already
constrained to `[a-z0-9-]`); user text never becomes a label value.

## 3.3 Loop heartbeats

Each background loop (`reaper`, `reconciler`) records, in process memory, when
its last pass finished, how long it took, and how many passes failed. These feed
`/readyz`, `/api/metrics`, and the Home System widgets. They are not persisted: after a
restart they start empty and readiness waits (up to the stale window) for the
first pass.

## 3.4 System widgets

`GET /api/system` (any valid token) returns the readiness checks, the same
numbers as `/api/metrics` in structured form, and the build version (the release
tag baked into the image, `0.0.0+dev` otherwise; [releases](releases.spec.md) 3.5). Home
renders it as its System widgets and refreshes with the event stream.

## 3.5 Disk guard

Source: the [disk guard draft](../drafts/2026-10-29.disk-guard.draft.md) and its
[investigation](../reports/2026-10-29.disk-guard-investigation.ref.md). Where the
investigation found a flaw, the corrected rule is stated and flagged **Decision**.

**Status — implemented:** measurement (data directory, `instances_root` when
configured, local Docker `DockerRootDir`), `st_dev` dedup, warn/refuse hysteresis,
one warning event per warn episode, typed `disk_low` claim admission from an
in-memory snapshot, authenticated read-only visibility, and the pause slice behind
`ARGUS_DISK_GUARD_PAUSE` (default off): `docker pause` / resume of descendant agent
containers, guard-side lease renewal, shed of the heaviest writer, the
failed-measurement pause, and writer ranking by cgroup `io.stat` with a size
fallback. **Not implemented:** a `timeout_s` exemption while paused (decided: none),
pause on remote daemons or Kubernetes (warn and refuse only), and any host-disk
filling. **Not verified:** the pause path has only synthetic tests (see
Verification boundary); real `docker pause`, real cgroup `io.stat` and the
disposable tmpfs scenario of the draft have not been run.

One guard loop measures free space and acts in bands. The 64 MiB `disk` readiness
check in 3.1 stays unchanged: data directory only, no effect from guard bands. The
guard loop keeps a `LoopStats` record for its own heartbeats but is **not** in the
`loops` dict `/readyz` iterates (open question 5); readiness checks stay exactly
database, reaper, reconciler, disk (64 MiB), secrets.

| Band | Default (setting) | Effect |
| --- | --- | --- |
| Warn | below 5 GiB, `5368709120` (`ARGUS_DISK_GUARD_WARN_BYTES`) | one warning event per episode; authenticated visibility shows it |
| Warn clear | above 6 GiB, `6442450944` (`..._WARN_CLEAR_BYTES`) | the episode ends |
| Refuse | below 2 GiB, `2147483648` (`..._REFUSE_BYTES`) | claims refused with typed admission reason `disk_low`; with `ARGUS_DISK_GUARD_PAUSE`, running descendant session containers paused (`docker pause`) |
| Resume | above 2.5 GiB, `2684354560` (`..._RESUME_BYTES`) | claims admitted; paused containers resumed and their pause cleared |
| Shed | still paused and below refuse after 30 s (`..._SHED_AFTER_S=30`) | stop the heaviest writer, `env_disk_full`, no attempt consumed; the rest stay paused |

Settings (validated at startup): `ARGUS_DISK_GUARD_ENABLED` (`true`),
`ARGUS_DISK_GUARD_INTERVAL_S` (`5`, must be `> 0`), the four byte thresholds above,
`ARGUS_DISK_GUARD_PAUSE` (`false`; boolean), `ARGUS_DISK_GUARD_SHED_AFTER_S` (`30`,
must be `> 0`) and `ARGUS_DISK_GUARD_FAIL_PAUSE_COUNT` (`2`, integer `>= 1`). An
empty or whitespace-only value means that setting's default. Startup requires
`clear > warn > resume > refuse >= 0`, `interval > 0` and the ranges above;
otherwise Argus refuses to start with a message that names the bad setting.

- **Hysteresis.** A band clears only at its upper edge: warn above 6 GiB, refuse
  above 2.5 GiB. Equality at a threshold does not change state (`free < warn`
  enters warn; `free > clear` exits; `free < refuse` refuses; `free > resume`
  readmits). Between refuse and resume the refusal holds.
- **Warning event persistence.** One `disk.warn` event is recorded per warn
  episode, carrying the free space measured when the episode began. A failed
  event write is logged and retried on the next pass until it succeeds (never
  duplicated); it never fails the pass, so a healthy boot with a warning band
  and an unhealthy event write still serves traffic.
- **Failed / unknown measurement.** A failed `stat` / `statvfs` / Docker-info
  query is state **unknown**, distinct from a measured 0. Unknown refuses new
  claims (fail closed). `ops.disk_free` returns 0 on `OSError`, so the guard must
  not reuse it. Before the first successful measured pass, state is unknown and
  claims are refused. When a failure follows a measured state, claims are refused
  immediately; the warn-episode flag is held until a later measured free crosses a
  hysteresis edge; if no successful measurement is newer than
  `max(3 × interval, 30 s)`, the snapshot is reported **stale** and claims stay
  refused. Recovery to a measured free `> resume` readmits.
- **Shed rule (Decision, changes the draft).** While claims are paused and free is
  still below refuse, shed at most one writer per `SHED_AFTER_S` period, the first
  `SHED_AFTER_S` after the pause. Shed is per container, which is per instance
  (one agent per NanoClaw instance). The victim container is stopped and its
  attempt ends `env_disk_full` (class `environment`): the task is `ready` again
  with `failures` unchanged, like a release, and an unshared account lease ends with
  it. The `disk.shed` event records the ranking basis. The other paused sessions
  stay paused. Resume is gated only on the 2.5 GiB band, never on "resume the
  rest": resuming while still below 2 GiB would re-pause on the next poll. Between
  refuse and resume claims stay paused and no further shed runs.
- **Failed-measurement pause.** **Decision (owner):** the draft pauses running
  sessions on the first failure; the guard pauses only after
  `ARGUS_DISK_GUARD_FAIL_PAUSE_COUNT` (2) consecutive failed passes. Admission
  fails closed on the first unknown. A failed measurement never sheds: shed needs a
  measured free below refuse.

### Pause, leases, and stall detection

**Status — implemented.** The guard loop sets and clears the pause, renews leases
and resumes, as below; shed is under the band table.

- **Guard-authoritative state.** The pause lives in the database: nullable
  `tasks.paused_disk_at`, set and cleared only by the server-side service API
  (`mark_paused_disk` / `clear_paused_disk`, per `(task id, attempt)`; clear also
  takes "all"). Each touches only the current attempt of a `claimed` task. No HTTP
  route, MCP tool, heartbeat field or `LeaseObservation` value sets, clears or
  implies it; `LeaseObservation` is unchanged. Every claim exit (complete, fail,
  release, expire, requeue, …) and every new claim clear it, so it never reaches
  another attempt.
- A paused session cannot heartbeat, so the guard renews paused claims' leases
  itself (`renew_paused_disk`, reason `paused_disk`), each by its `lease_ttl_s`,
  at half the shortest paused TTL. An already expired lease is not revived; an
  unrenewed paused lease expires as any other.
- A heartbeat alone does not stop quiet or stalled: a paused container stops
  writing `activity_at`. `flag_quiet` and `expire_stalled` skip claims with
  `paused_disk_at` set, as a SQL condition in their own write transaction, so a
  pause or resume and an expiry never interleave.
- **Resume restarts the quiet clock.** Clearing the pause sets `activity_at` to
  the resume time and clears `quiet_at`: time spent paused never counts toward
  quiet or stalled. No other worker signal is written.
- `stall_cause` gains `paused_disk` for visibility only, derived from the column,
  never from an observation (task read, inbox, expiry errors and events). No worker
  reports the pause as authority; the API knows because it paused the claim.
- **Decision:** the wall-clock `timeout_s` is not exempt while paused: a paused
  claim still times out.

### Who runs the loop

**Decision:** the API process owns the guard loop. Single node; it is one more
asyncio task with a `LoopStats` record next to the cleanup loop (not registered in
the readiness `loops` map). The NanoClaw host needs no loop. Measurement runs off
the event loop (`asyncio.to_thread`) and never while holding the DB writer; the
claim path reads only the in-memory snapshot.

The pause hook runs inside the loop, after each measurement. It never pauses the
Argus-started host container (it sends lease heartbeats); it pauses only
descendant agent containers found by `descendant_label`, and only on a local
unix-socket daemon (`shares_filesystem`). Remote daemons and Kubernetes get the
warn and refuse bands only. A restarted API adopts containers it finds already
paused and resumes them with the rest.

### "Wrote most"

**Decision, implemented:** rank by cgroup v2 `io.stat` `wbytes` delta.

- Rank by the `io.stat` `wbytes` delta per container, sampled at pause time and at
  shed time. A frozen cgroup only writes back dirty pages, so the delta is a
  proxy for who was writing, not a byte-exact total.
- Take one bounded directory-size sample of the container's instance directory at
  pause time (the workspace-cleanup bounded walker), off the event loop: tie-break
  and the number shown in the event. The loop does no other directory walk.
- Reject `docker ps --size`: it reads the writable layer, is slow, and cannot see
  bind-mount writes.
- If `io.stat` is unreadable (controller not enabled for the Docker slice, or no
  cgroup v2), rank by directory size and say "ranked by size" in the `disk.shed`
  event. Not yet verified on a real Docker host.

### Filesystems

Judged separately; the worst one decides. Paths on the same `st_dev` form one group
and one episode; a group containing the data directory counts as the data directory.
Pause and shed act on the role union of the groups below refuse; a group that
contains the data directory counts as the data directory. The 64 MiB readiness
check reads the data directory alone, and limits use the worst free among groups.
Unknown beats every measured free; otherwise the minimum free among applicable
groups decides the band. Bounded: a constant number of `stat` / `statvfs` /
Docker-info calls per pass; no directory walks; Docker query with a short timeout.

- The data directory (always applicable).
- Task workspaces and checkpoints (`instances_root`) when configured; otherwise
  not applicable. A configured root that does not exist yet is absent (holds no
  data) and counts as not applicable, not as a failed measurement.
- The Docker data root (`DockerRootDir`): applicable only when a local unix-socket
  daemon is configured (socket present / `DOCKER_HOST` is unset or `unix://`,
  using Argus's configured docker socket when `DOCKER_HOST` is unset); remote or
  not configured → not applicable. A `DockerRootDir` this process cannot `stat`
  does not share its filesystem (the API-in-a-container case) → not applicable. A
  path that `stat`s but whose `statvfs` fails stays unknown and refuses claims.
  An unreadable Docker-info answer from an applicable daemon → unknown (not a
  measured 0), which refuses claims.

### Visibility

An authenticated surface (system summary) reports, per root role (`data`,
`instances`, `docker`), applicability (`measured` / `unknown` / `stale` /
`not_applicable`), free bytes when known, band, plus global claim admission
(`admitted` / `refused`) and warn-episode state. When the guard is disabled,
measurement is `disabled` (not a measured free of null). Per-root `band` is the
instantaneous free-space band for that root; global `claim_admission` is latched
hysteresis (refuse until free clears the resume edge, and refuse while unknown
or stale). An operator can therefore see a root band of `warn` (or even `ok`)
next to `claim_admission: refused` — the band is not the admission latch. It must
not include filesystem paths, `st_dev` values, container/daemon identifiers, or
the Docker root path. `/readyz` and `/healthz` stay unauthenticated and carry no
paths or identifiers.

### Verification boundary

Automated tests are synthetic: injected `statvfs` / `stat` / clock / Docker-info /
`io.stat` / directory-size fixtures, a fake container target, and disposable
temporary directories. They do not prove behaviour against a real host disk, a live
Docker daemon (`docker pause`, cgroup `io.stat`), or production free-space
conditions. The draft's disposable tmpfs test has not been run.

### Open questions (owner decisions)

Decided and implemented: shed requeues without consuming an attempt
(`env_disk_full`); defaults `ENABLED=true`, `PAUSE=false`; `timeout_s` is not exempt
while paused; the API process owns the loop; the two draft changes (resume gated on
2.5 GiB only, failed-measurement pause threshold). Still open:

1. Absolute thresholds, or a percentage floor on small disks.
2. Whether guard loop staleness fails `/readyz`.
3. Data-directory band: pause and shed when sessions may not be the writers.

# 4. Open Questions

1. A read-only `metrics` token scope instead of requiring `admin` for scrapers.
2. Request-level metrics (rate, latency by route) if needed in practice.

# 5. Constraints

- No new required dependency or service. Tests may use `prometheus-client` to
  validate the exposition format; the runtime does not.
- `/readyz` never takes the writer and costs at most one probe per 2 s.
- `/api/metrics` and `/readyz` are bounded: a fixed number of aggregate queries, no
  per-row scans, no filesystem walks.
- `/readyz` must stay unauthenticated and free of identifiers, because probes and
  load balancers call it.

# 6. Implementation Consequences

- `argus_api.ops` holds `LoopStats`, the collector, and the metric rendering;
  the reaper and reconciler wrap each pass with a `LoopStats` record.
- `/readyz` replaces nothing: it sits beside `/healthz`.
- CI gains manual jobs for the real-Docker and real-Kubernetes (kind) integration
  tests; the production image builds `sops` for both amd64 and arm64.
- Quality CI builds `deploy/Dockerfile.api`, loads it, and smokes `/healthz`,
  `/readyz`, and `/data/admin-token`; it also builds `deploy/nanoclaw-host` and
  `examples/echo-worker`. The NanoClaw agent image stays a host build
  (`provision.sh`). Operator quick-start: `deploy/compose.yaml` (README).
