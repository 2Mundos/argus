# Dashboard live data performance

# 1. Purpose and Scope

Keep the tasks board responsive with hundreds of tasks and frequent live events.

# 2. Non-Goals

No persistent browser cache, new server dependency, or engine-specific behaviour.

# 3. Decisions Made

- Keep the current query in memory while refreshing. Subscribe each query to events
  that affect its data; batch relevant events within 250 ms without postponing a
  refresh indefinitely under continuous traffic. Logs and workspace changes do not
  invalidate snapshots. Reconnects and explicit reloads fetch a fresh snapshot.
- The board ignores non-task subjects, including account lease events. For
  changes to tasks already on the board, fetch summaries through the existing REST batch read
  (`GET /tasks?ids=...`, at most 50 IDs per request), in parallel, and replace only
  those entries. New tasks, missing tasks and archive membership changes require
  a full snapshot to preserve totals and the newest-500 boundary. Retain unresolved
  event invalidations until a response is accepted, so a
  superseding delta also reads tasks from the request it replaces. An incomplete
  initial snapshot is always replaced by a full snapshot.
- Snapshots read two independent cursor chains in parallel (`GET /tasks?state=...`):
  the active states (waiting, ready, running, in review), then the ended ones
  (done, failed, cancelled), each newest first and capped at 500. The board paints
  as soon as the first active page is in; until the ended tasks arrive their columns
  show an unknown count, not zero, and the "newest N of M" notice is withheld.
  Within one chain, cursors depend on the previous response and are fetched in order.
- Memoize cards using displayed fields. A single board clock updates every 30
  seconds; only running cards receive its ticks.
- Header, onboarding and worker queries filter their own relevant event families.
  Other views retain broad invalidation until their dependencies are audited.
- One read per resource on the shell. The workers rail and worker chips share the
  worker provider's agent list; the setup host takes the shell's onboarding status;
  the setup wizard reads its status and template catalog only while open. Over
  HTTP/1.1 the browser allows six connections per host, one held by the event
  stream, so every duplicate startup read delays the board's first page.
- The task detail paints from its own task read; artifacts, events, delivery,
  models and the attempt strip each have their own query, so a slow forge read or
  a large read never holds the whole view. The attempt strip reads
  `GET /tasks/{id}/attempts/summary` (attempt numbers and `steer_unknown`, without
  the stored definitions). One steers source serves both the task actions row and
  the Live panel: one steers read and one assignee lookup per open task.
- The server gzips responses of 1 KiB or more (JSON and dashboard assets), except
  the event stream, already-compressed and opaque binary types, and ranged reads.

# 4. Open Questions

Persistent cache reuse across navigation needs explicit user scoping and freshness
rules. Task pages are capped at 100 rows and chain through cursors, so the ended
tasks still cost one sequential round trip per 100 (three at 239); a larger cap or a
parallel-safe cursor would need an API change. The agent list still includes every
ended worker.

# 5. Constraints

The live preview and its database remain read-only. Existing REST and MCP contracts
remain unchanged. All engines share the same dashboard behaviour.

# 6. Implementation Consequences

Implement event filtering and optional delta refreshes in `useLiveData`, shared by
queries. The task detail gives each section its own query, and the attempt strip
reads `GET /tasks/{id}/attempts/summary`; that route adds a schema and generated
client type (`make openapi`).

## Verification boundary

- Commands: `pnpm --filter @argus/dashboard test`,
  `pnpm --filter @argus/dashboard typecheck`, `pnpm build`,
  `make openapi-check`, `python3 scripts/validate_docs.py`.
- Verified: 599 dashboard tests, the Python suite, type checking, the production
  build, OpenAPI freshness and documentation validation. The task detail paints
  while the delivery read is still pending, two steers copies share one read, and
  the attempts summary answers with only `attempt` and `steer_unknown`. Chromium
  comparison against the original source with 262 synthetic ready tasks: account
  lease events issue zero board requests (previously three); a title update reads
  one summary and renders one card (previously three pages and 524 card renders).
  Board clock intervals fall from 262 to one. Initial loading remains three cursor
  pages. Synthetic payload for the title update falls from 54,187 to 264 bytes;
  this is not a live payload size. Browser checks also cover archive membership,
  creation, reconnect refresh and overlapping updates (the superseding batch
  includes both unresolved IDs).
- Profiled before this change (production build against the remote preview, 262
  tasks, HTTP/1.1, about 0.6 s per round trip): twelve startup reads (three of
  onboarding, two of agents at 99 KB each) queued ahead of the board; the first
  cards appeared at about 3.1 s and the last page at about 5.8 s, with 360 KB of
  uncompressed task JSON. Dedupe and gzip target exactly these.
- Not verified: production latency or CPU improvement with real active tasks;
  the browser comparison used a disposable fixture, not the live preview. The
  task-detail change is measured by request behaviour, not wall-clock time in a
  browser.
