# Home

# 1. Purpose and Scope

Home is the first dashboard tab (`#home`) and the default for an empty or invalid
hash. It answers three questions without opening another tab: what needs a person
or an operator now, how big is each part of the fleet, and what happened lately.

It shows, in order:

1. a status bar (readiness verdict, event stream, age of the totals);
2. a prioritized **Needs attention** list;
3. compact summary groups (Tasks, Agents, Pools, Inbox, Accounts), each linking to
   its existing tab;
4. **Recent activity** (the latest 10 events, link to Events);
5. the **System** widgets (readiness checks, background loops, work, agents, pools,
   storage and build version), formerly the System tab;
6. an expandable **All states** section: every state of every core enum, zeros included.

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
  - attention: degraded check; pool with `running < desired`; exhausted accounts;
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
- Frontend: `views/Home.tsx`, `overview.ts` (types and attention rules), `getJson`
  in `api.ts`, `home` first in the `App.tsx` tabs.
- After `make openapi` and regenerating `packages/api-client/src/schema.d.ts`,
  replace the local `Overview` types with `components["schemas"]["Overview"]` and
  drop `getJson`.
