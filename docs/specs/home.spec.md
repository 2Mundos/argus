# Home

# 1. Purpose and Scope

Home is the first dashboard tab (`#home`) and the default for an empty or invalid
hash. It answers three questions without opening another tab: what needs a person
or an operator now, how big is each part of the fleet, and what happened lately.

The authenticated dashboard is a three-column shell on every route: a left
navigation, the page, and a right rail of live workers only. Recent non-transient
events appear in the bottom live status bar ([live work](live-work.spec.md) 3.4),
not in the rail. The stream status and sign-out sit in the page top bar. A server-status
box sits at the bottom of the left navigation. It starts collapsed, showing
only the verdict, and a button expands it to every readiness check. Navigation
reaches every existing tab (Home, Tasks, Inbox, Agents, Pools, Accounts, and
the remaining Knowledge, Fleet, Engines, Cost, Events and Settings links).
There is no workspace switcher and no time-range control.

The top bar jumps to an existing page. It is not a search of task text and it
is not a time-range control. At the bottom of the left navigation, a collapsed
server-status box shows the verdict from `GET /api/system` (ready when every
readiness check is ok, degraded or failing when a check says so, unknown while
that read is missing). A button expands it to one row per check, with its
status and detail, plus version and uptime.

Home splits that into two panels on the same page. It does not repeat the
Tasks list; that list lives on the Tasks tab.

1. **Overview** — five larger summary cards (Tasks, Agents, Pools, Inbox and
   Accounts), each listing every state in that group (zeros included) plus the
   total and linking to its existing tab (no chart). The card header is an icon,
   the label, and the total at the end of that row. The Accounts card says
   quota is not measured by Argus. A **Needs attention** card appears
   only when an item exists or a source could not be read (a fully loaded empty
   fleet does not claim an all-clear sentence). The card starts collapsed and
   shows a count per severity (critical, attention, info). A button expands the
   item list. **Recent activity** is a table of
   the latest events (time, type, detail, status, actor) with a link to Events;
   and navigation-only shortcuts to create a task, open Agents, open Pools,
   open Inbox and open Events.
2. **System** — cards from `GET /api/system`, not another copy of the Overview
   counts. Background loops sit in one card, with the sentence that heartbeats
   prove a process is alive. Beside it, a card lists the oldest ready task, open
   decisions and events written, and a storage card lists database size, WAL size
   and disk free bytes (not a percentage). A pools card lists each pool's live,
   desired and cap. Task and agent state counts stay on Overview and inside **All
   states**. Version and uptime stay in the navigation box. **All states** starts
   open: every state of every core enum, zeros included, and it can still be
   collapsed. The readiness checks themselves live in the collapsed navigation box.

Heartbeats and `running` still prove only that a process is alive. Home says so.

Totals come from one read-only endpoint, `GET /api/overview`, readable by the
manager, orchestrator and admin scopes (never workers). The dashboard uses the API
only and the existing live stream: it refetches when events arrive and after every
reconnect. Becoming live the first time is not a reconnect, so it does not start
another fetch or discard the one already in flight. A refetch leaves data on screen
until the new response arrives; a view that is still empty shows the first response
that returns.

# 2. Non-Goals

- Not a metrics or time-series system; no history charts.
- No quota, token or spend figures on Home: they live on the Accounts tab
  ([account usage](account-usage.spec.md)). Home adds no near-limit alert.
- No model/LLM availability verdict. Heartbeats and `running` prove a process is
  alive, not that a model answers; Home says so and does not infer more.
- No filters on links (no view supports them yet), no new tables, migrations,
  services or dependencies, and no write actions.
- No invented time windows or thresholds.

# 3. Decisions Made

- **One snapshot.** The endpoint runs every `GROUP BY` in one read transaction, so
  the numbers agree with each other. Totals are whole-table counts, independent of
  list page sizes (`/api/tasks` caps at 500; Home does not).
- **All states, always.** Every member of `TaskState`, `AgentState`, `Role`,
  `PoolStatus`, `InstanceState`, `AccountState`, `DecisionState`, `DecisionKind`,
  `Audience`, `PageState`, `ProposalState` appears with an explicit `0` when empty.
- **0, unknown and error are different.** `0` is a measured zero. A source that is
  loading shows a loading note, a failed one shows its error with Retry, and a
  source that has data but whose refresh failed (or the stream is down) is marked
  stale. Attention never reads "Nothing needs attention" while a source is missing.
- **Current versus history.** `agents.by_state.lost` and `instances.by_state.terminated`
  are history and live only in All states. The attention signal is
  `agents.lost_on_unfinished_instance`: lost agents whose instance is not yet
  terminated, i.e. a rollout still in progress. A replaced, healthy rollout therefore
  raises no standing alert.
- **Failed tasks are info.** Nothing records whether a failure was handled and no
  window is defined, so `failed` is informational and never critical.
- **Attention rules** (severity, then items needing a person first, then count):
  - critical: a failing readiness check; a pool whose status is not `ok`; a provider
    with accounts but none `available`.
  - attention: degraded check; pool with `running < desired`; exhausted accounts; an account whose last rejection is newer than its last
    acceptance (or rejected, never accepted; from `GET /api/accounts`, skipped when
    the caller cannot read it, [account usage](account-usage.spec.md) 3.5);
    lost agents on unfinished instances; blocked tasks; tasks awaiting approval;
    open decisions; stream offline; a source that could not be loaded.
  - info: tasks in review; failed tasks; terminating instances; open knowledge proposals.
- **`holding_claims`** counts distinct live agents with a claimed task. It is not a
  health statement.
- **No secrets.** The payload holds counts, enum values, provider and pool names and
  one timestamp; no credentials, secret references or free text.
- Home keeps every other tab, `#tasks` included.

## Endpoint contract

`GET /api/overview` → `Overview` (401 without a valid token, 403 for workers).
Authoritative models: `apps/api/src/argus_api/overview.py`.

```
as_of: datetime
tasks: {state: int}                       # all TaskState members
agents: {by_state, by_role: {role: {state: int}}, holding_claims, lost_on_unfinished_instance}
pools: {by_status, total, desired, max, live, running,
        items: [{name, status, desired, max, live, running}]}
instances: {by_state, draining}
accounts: {by_state, total, by_provider: {provider: {state: int}}, next_reset_at, quota_measured}
decisions: {by_state, open_by_kind, open_by_audience}
knowledge: {pages_by_state, proposals_by_state}
```

# 4. Open Questions

- Should failed tasks page after a defined window or once acknowledged? Needs an
  owner's decision and probably a field recording acknowledgement.
- Links go to bare tabs; deep links with filters need view-level filter support.

# 5. Constraints

- Single node, SQLite only: no new service, table, migration or dependency.
- Reads only; it must never emit events or take the write lock.
- UI text is English. Layout works from 390 px to 1440 px, tables scroll inside
  their container, severity is conveyed by icon and word as well as colour, and the
  attention region is `aria-live="polite"`.
- The generated OpenAPI schema does not yet contain `/api/overview`; the dashboard
  uses local types in `apps/dashboard/src/overview.ts` until regeneration.

# 6. Implementation Consequences

- Backend: `apps/api/src/argus_api/overview.py`, route in `ops_routes.py`, tests in
  `apps/api/tests/test_overview.py`.
- Frontend: `views/Home.tsx`, `overview.ts` (types and attention rules), the
  shell in `App.tsx` (left navigation, top bar, workers rail, live status bar).
- After `make openapi` and regenerating `packages/api-client/src/schema.d.ts`,
  replace the local `Overview` types with `components["schemas"]["Overview"]` and
  drop `getJson`.
