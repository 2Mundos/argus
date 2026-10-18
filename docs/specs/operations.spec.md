# Operations Spec

Health, readiness, metrics, and the System view. Everything here follows
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
and the dashboard's System tab.

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
  | `database` | a write transaction does not complete within 5 s | A wedged single writer is the failure that matters; a read alone would not show it. |
  | `reaper` | no pass completed within `max(3 × interval, 30 s)` | Leases would stop expiring. |
  | `reconciler` | same | Pools would stop converging. |
  | `disk` | free space on the data directory below 64 MiB | SQLite cannot write. |
  | `secrets` | *degraded*, never failing, when the secret store failed to load | Optional by design (single-node 3.5). |

  Readiness is `503` only when a check is `failing`; `degraded` still returns
  `200`. The body carries no secrets and no identifiers.

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
`/readyz`, `/api/metrics`, and the System view. They are not persisted: after a
restart they start empty and readiness waits (up to the stale window) for the
first pass.

## 3.4 System view

`GET /api/system` (any valid token) returns the readiness checks, the same
numbers as `/api/metrics` in structured form, and the build version. The dashboard's
System tab renders it and refreshes with the event stream.

# 4. Open Questions

1. A read-only `metrics` token scope instead of requiring `admin` for scrapers.
2. Request-level metrics (rate, latency by route) if needed in practice.

# 5. Constraints

- No new required dependency or service. Tests may use `prometheus-client` to
  validate the exposition format; the runtime does not.
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
