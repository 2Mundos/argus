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

**Target behaviour, not implemented.** Source: the
[disk guard draft](../drafts/2026-10-29.disk-guard.draft.md) and its
[investigation](../reports/2026-10-29.disk-guard-investigation.ref.md). Where the
investigation found a flaw, the corrected rule is stated and flagged **Decision**.

One guard loop measures free space and acts in bands. The 64 MiB `disk` readiness
check in 3.1 stays unchanged: data directory only, no effect from guard bands.

| Band | Default (setting) | Effect |
| --- | --- | --- |
| Warn | below 5 GiB, `5368709120` (`ARGUS_DISK_GUARD_WARN_BYTES`) | one warning event per episode; Home shows it |
| Warn clear | above 6 GiB, `6442450944` (`..._WARN_CLEAR_BYTES`) | the episode ends |
| Refuse | below 2 GiB, `2147483648` (`..._REFUSE_BYTES`) | claims refused with typed admission reason `disk_low`; running session containers paused (`docker pause`) |
| Resume | above 2.5 GiB, `2684354560` (`..._RESUME_BYTES`) | paused containers resumed; claims admitted |
| Shed | still paused after 30 s (`..._SHED_AFTER_S=30`) | stop the heaviest writer, `env_disk_full`, no attempt consumed; the rest stay paused |

Other settings: `ARGUS_DISK_GUARD_ENABLED` (`true`), `ARGUS_DISK_GUARD_PAUSE`
(`false`: no `docker pause`, no shed), `ARGUS_DISK_GUARD_INTERVAL_S` (`5`),
`ARGUS_DISK_GUARD_FAIL_PAUSE_COUNT` (`2`).

- **Hysteresis.** A band clears only at its upper edge: warn above 6 GiB, refuse
  above 2.5 GiB. Startup validation requires `clear > warn > resume > refuse`;
  otherwise Argus refuses to start.
- **Shed rule (Decision, changes the draft).** Shed at most one writer per
  30 s period. Resume is gated only on the 2.5 GiB band, never on "resume the
  rest": resuming while still below 2 GiB would re-pause on the next poll.
- **Failed measurement.** A failed `statvfs` counts as below 2 GiB: new claims are
  refused (fail closed). `disk_free` returns 0 on `OSError` today, so the guard
  must not reuse it; its variant distinguishes "failed" from "0 free".
  **Decision (owner):** the default pauses running sessions on the first failure;
  the investigation recommends pausing only after 2 consecutive failures
  (`ARGUS_DISK_GUARD_FAIL_PAUSE_COUNT=2`), since one transient error would pause
  every session.

### Pause, leases, and stall detection

- A pause renews the claim's lease with an explicit `paused_disk` heartbeat reason,
  so the lease is not treated as lost. Heartbeats have no reason field today, and
  `LeaseObservation` `session` is a closed enum (`running|gone|unknown`), so the
  reason/session value is new.
- A heartbeat alone does not stop quiet or stalled: a paused container stops
  writing `activity_at`. Therefore the guard exposes the set of task ids it has
  paused to the reaper; quiet and `expire_stalled` skip those claims.
- `stall_cause` gains `paused_disk` for visibility only. No worker reports the
  pause as authority; the API knows because it issued `docker pause`.
- The wall-clock `timeout_s` is not exempt while paused unless the owner decides
  so (open question).

### Who runs the loop

**Decision:** the API process owns the guard loop. Single node, and it issues
`docker pause`; it is one more asyncio task with `LoopStats` next to the cleanup
loop. The NanoClaw host needs no loop.

- Never pause the Argus-started host container: it sends the lease heartbeats.
  Pause only descendant agent containers found by `descendant_label`.
- Pause only on a local unix-socket daemon (`shares_filesystem`). Remote daemons
  and Kubernetes get the warn and refuse bands only.

### "Wrote most"

- Rank by the cgroup v2 `io.stat` `wbytes` delta per container, sampled at pause
  time and at shed time.
- Take one bounded directory-size sample of the container's instance directory at
  pause time (the workspace-cleanup bounded walker): tie-break and the number shown
  in the event.
- Reject `docker ps --size`: it reads the writable layer, is slow, and cannot see
  bind-mount writes.
- **OPEN:** verify the `io` controller is enabled for the Docker slice and that
  `wbytes` is readable on a Docker host. If unreadable, fall back to directory
  size and say "ranked by size" in the event.

### Filesystems

Judged separately; the worst one decides. Paths on the same `st_dev` form one group
and one episode; a group containing the data directory counts as the data directory.

- The data directory.
- Task workspaces and checkpoints (`instances_root`).
- The Docker data root (`DockerRootDir`), local daemon only; otherwise not applicable.

### Open questions (owner decisions)

1. Shed outcome: `blocked` (existing `environment` class) or requeue without an
   attempt (recommended; new rule).
2. Defaults `ENABLED=true`, `PAUSE=false` (recommended).
3. Absolute thresholds, or a percentage floor on small disks.
4. Whether `timeout_s` is exempt while paused.
5. Whether guard loop staleness fails `/readyz`.
6. Data-directory band: pause and shed when sessions may not be the writers.
7. Accept the two draft changes: resume gated on 2.5 GiB only; failed-measurement
   pause threshold.

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
