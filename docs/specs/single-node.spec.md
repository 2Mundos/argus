# Lightweight Single-Node Spec

This is a core principle of Argus. Every other spec and every new dependency is
judged against it.

# 1. Purpose and Scope

Argus must start small: **one process, one node, one data directory**. A single
`docker run -v argus-data:/data argus` (or one binary-like process on a laptop
or a 1 vCPU / 1 GB VM) gives a complete, working control plane: API, MCP
server, dashboard, background loops, state, and artifacts.

"Single-node" applies to the **Argus control plane**. The fleet it manages may
still run elsewhere (a remote Docker host or a Kubernetes cluster, see
[worker lifecycle](worker-lifecycle.spec.md) 3.5); Argus itself does not.

# 2. Non-Goals

- High availability, multi-replica API, leader election, or multi-writer
  databases. A restart of the one process is the recovery model.
- Horizontal scaling of the control plane. When one node is not enough, that is
  a future spec, written against measured load.
- Built-in log aggregation, metrics stacks, or search clusters.

# 3. Decisions Made

## 3.1 One process

The API process hosts everything: REST, the SSE stream, the MCP endpoint, the
reaper, the reconciler, and the built dashboard (static files served by the
API). Background loops are asyncio tasks in that process, not separate services
or a job queue. Concurrency inside the process is plain asyncio.

At startup Argus takes an exclusive lock file in the data directory and exits if
another process holds it. A second Argus on the same data is always a bug.

## 3.2 SQLite + Litestream

- State lives in one SQLite database in WAL mode (`/data/argus.db`).
- All writes go through **one writer connection**, serialized in-process, each
  in a `BEGIN IMMEDIATE` transaction. Reads use a small pool of read
  connections. This makes write serialization explicit and removes the need for
  row locks, `SKIP LOCKED`, or lock ordering.
- **Litestream** continuously replicates the WAL to a replica (S3-compatible
  bucket, SFTP, or a local path). On boot, if the database is missing and a
  replica is configured, Argus restores from it before opening. Litestream is
  optional: without it Argus runs fine, just without off-node backups.
- Migrations are plain numbered `.sql` files applied by a small runner that
  tracks `PRAGMA user_version`. No ORM is required.
- Time comes from one clock: the Argus process, in UTC, read **after** the
  write transaction has started. Worker clocks never validate anything.

## 3.3 In-process event bus

Committed transactions publish their events to an in-memory bus after commit.
Long-polling claims and SSE subscribers wait on that bus. The `events` table
remains the durable record: SSE clients resume from `Last-Event-ID` by reading
the table, so a restart loses no history. No `LISTEN/NOTIFY`, Redis, or broker.

## 3.4 Local filesystem for bytes

Artifacts are files under `/data/artifacts/`, uploaded and downloaded through
the API with a configurable size limit. They sit behind an `ArtifactStore`
interface so an S3-compatible backend can be added later, but none is required.
Container logs are not stored: the dashboard fetches them live from the target
(Docker logs API, Kubernetes pod logs) while the container exists. Agents that
need durable transcripts upload them as artifacts.

## 3.5 Optional means off by default

Any integration that needs another service (S3, a secret manager, OpenTelemetry
export, chat notifications) is optional, configured explicitly, and disabled by
default. Argus never fails to start because an optional service is missing.

## 3.6 Footprint budget

Targets, checked as the code grows:

- One container image for the control plane.
- Idle memory under 200 MB; startup under 3 s on a small VM.
- Zero required external services; Docker access only when a `docker` target is
  configured.

# 4. Open Questions

1. Backup for `/data/artifacts`: Litestream covers only SQLite. Is a documented
   `restic`/`rsync` job enough until an S3 artifact store exists?
2. At what measured load (agents, tasks per second, database size) do we revisit
   this spec?

# 5. Constraints

- A proposed dependency that adds a required service is rejected unless this
  spec is changed first, in its own PR.
- SQL stays simple and mostly portable (no SQLite-only tricks without need), so
  a future move to a server database remains a migration, not a rewrite. This is
  a preference, not a promise.
- Write transactions must be short. No network calls inside a write
  transaction; provider calls happen after commit (see worker lifecycle 3.4).

# 6. Implementation Consequences

- `apps/api` serves `apps/dashboard`'s build output; the production image is
  multi-stage (Node build, then Python runtime) and ships Litestream.
- The data layer is a thin repository module over `sqlite3`/`aiosqlite`, with
  one writer and a reader pool; tests use a temporary database file per test.
- No `docker-compose.yml` is needed for development: `make dev-api` creates
  `./data/argus.db` on first run.
