---
status: implemented
created: 2026-11-04
updated: 2026-11-11
---

# Dashboard navigation

# 1. Purpose and Scope

The dashboard's main menu is short enough to read at a glance. Pages that belong
together share one menu entry and switch between each other with tabs at the top of
the page. On a narrow screen the menu moves to a bar across the top.

# 2. Non-Goals

- No new pages, and no change to what any page shows.
- No change to the live status bar. There is no page search and no top bar of its
  own: the menu replaces them.
- No breaking of links: every existing hash (`#setup/accounts`, `#workers/pools`, the
  legacy single-word ones) keeps opening the same page.

# 3. Decisions Made

- **Nine entries**, in this order: Chat, Dashboard, Tasks, Inbox, Projects,
  Knowledge, Routines, Workers, Setup. Tasks and Inbox keep their counts. There is
  no "More" section.
- **Workers** groups Agents, Pools and Fleet. **Setup** groups Settings, Accounts,
  Engines, Cost and Events. The menu entry is current on any of its tabs and links to
  the group's first tab (`#workers` opens Agents, `#setup` opens Settings).
- **Tabs** are links (`#group/tab`) in a `nav` above the page, the current one marked
  `aria-current="page"`; they are not a client-side switch, so back/forward, reload
  and shared links keep working.
- **Sign out** is the last item of the menu column, under the server status box. The
  Inbox bell and the "Live" label are gone: the Inbox entry carries the count, and
  the live status bar at the foot of the page shows the connection.
- **Narrow screens** (1100 px and below): the menu is one row across the top, brand
  first, the entries scrolling sideways if they do not fit, Sign out at the end;
  below 960 px only icons show, each keeping its text as its accessible name. The
  server status box is not shown there.
- **Projects** opens a project at `#projects/<slug>` and its tabs at
  `#projects/<slug>/<tab>` (Overview, Graph, Task defaults: [task
  defaults](task-defaults.spec.md) 3.7, [task graph](task-graph.spec.md) 3.2); Settings
  opens a project's scope at `#setup/settings/<slug>`.
- **Unsaved edits** hold navigation: a hash change away from a form with unsaved
  edits asks first, and "no" puts the old hash back.
- Every entry has its own icon.
- **The wordmark in the menu is a link home**: `#chat` with the orchestrator on,
  `#dashboard` without it, the same page the app opens on. The login screen's stays
  plain text.
- **A hash may carry a query string** (`#tasks?state=failed&q=retry`, the Tasks board state filter and search of
  [task discovery](task-discovery.spec.md) §3.5). Routing, `isCurrent` and the
  default-hash rewrite look only at the part before `?`.

# 4. Open Questions

- Whether Routines belongs under Setup. It stays top level while it is an
  everyday page.
- Whether the server status box should come back on narrow screens, for example
  in the page header.

# 5. Constraints

Dashboard only; no API, schema or wire change. English copy. Tests do not sleep or
poll.

# 6. Implementation Consequences

`App.tsx` (menu, sign out, `isCurrent`, tab bar), `unsaved.ts` (the hash and the
unsaved-edits guard) and the layout and tab styles in `styles.css`.

## Verification boundary

- Commands: `pnpm --filter @argus/dashboard test`,
  `pnpm --filter @argus/dashboard typecheck`, `python3 scripts/validate_docs.py`.
- Verified in a browser at desktop width, 900 px and 360 px.
- Not verified: touch devices.
