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
- Initial snapshots retain cursor pagination and progressively show their pages.
  Cursors depend on previous responses and cannot safely be fetched in parallel.
- Memoize cards using displayed fields. A single board clock updates every 30
  seconds; only running cards receive its ticks.
- Header, onboarding and worker queries filter their own relevant event families.
  Other views retain broad invalidation until their dependencies are audited.

# 4. Open Questions

Persistent cache reuse across navigation needs explicit user scoping and freshness
rules. Smaller initial payloads may be useful after browser profiling.

# 5. Constraints

The live preview and its database remain read-only. Existing REST and MCP contracts
remain unchanged. All engines share the same dashboard behaviour.

# 6. Implementation Consequences

Implement event filtering and optional delta refreshes in `useLiveData`, shared by
queries. No API schema or generated client changes are required.

## Verification boundary

- Commands: `pnpm --filter @argus/dashboard test`,
  `pnpm --filter @argus/dashboard typecheck`, `pnpm build`,
  `python3 scripts/validate_docs.py`.
- Verified: 544 existing dashboard tests, type checking, production build and
  documentation validation. Chromium comparison against the original source with
  262 synthetic ready tasks: account lease events issue zero board requests
  (previously three); a title update reads one summary and renders one card
  (previously three pages and 524 card renders). Board clock intervals fall from
  262 to one. Initial loading remains three cursor pages. Synthetic payload for
  the title update falls from 54,187 to 264 bytes; this is not a live payload size.
  Browser checks also cover archive membership, creation, reconnect refresh and
  overlapping updates (the superseding batch includes both unresolved IDs).
- Not verified: production latency or CPU improvement with real active tasks;
  the browser comparison used a disposable fixture, not the live preview.
