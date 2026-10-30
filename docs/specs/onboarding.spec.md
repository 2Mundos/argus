# Onboarding

# 1. Purpose and Scope

A newcomer on a fresh install meets an empty dashboard: Accounts says "No
accounts yet", and nothing tells them that a worker needs an account and a pool
before any task runs. This spec adds an **onboarding wizard** that walks an admin
through the shortest path to a working Argus, and renames Home so the chat can
become the main page ([internal orchestrator](internal-orchestrator.spec.md) 3.8).

In scope:

- The wizard: three steps, when it opens, how progress is known.
- `GET /api/onboarding` and the one stored setting.
- Routing: `#home` → `#dashboard`, and the default route.

# 2. Non-Goals

- **No new account or pool backend.** Each step reuses the existing forms and
  routes: the per-provider account import/generate flows
  ([Claude](claude-accounts.spec.md), [Codex](codex-accounts.spec.md)) and the
  guided worker template flow ([worker launch](worker-launch.spec.md)).
- **No stored progress.** Steps are done when the facts say so (3.2).
- **Not for agents.** Orchestrator and worker tokens never see the wizard; it is
  dashboard-only, with no MCP equivalent.
- No change to what Home shows; only its name and route change.

# 3. Decisions Made

## 3.1 Steps

1. **Add an account.** Offers the harness-capable providers, Claude and Codex,
   with their existing import/generate forms. A link opens the Accounts tab for
   the others (Cursor, Copilot, custom), which cannot carry a worker alone.
2. **Create a first worker.** The guided template form from the Agents view,
   unchanged (`GET /api/fleet/templates`, `POST /api/fleet/workers`).
3. **Start the internal orchestrator** (optional, skippable). Shows what it does
   and costs, an editable `orchestrator.daily_budget_usd`, and an Enable button
   that writes `orchestrator.enabled = true`. If the `nanoclaw-orchestrator`
   template is not ready on the node, the step shows its blockers instead.

Each step shows the result of its existing route (errors included) and moves on
when its fact becomes true. A step already done shows as done and can be skipped.

## 3.2 Progress is derived: `GET /api/onboarding`

Admin only. Returns:

```json
{
  "account": true,
  "worker": false,
  "orchestrator": false,
  "dismissed": false
}
```

- `account`: at least one account of a harness-capable provider exists.
- `worker`: at least one non-system pool exists (the orchestrator's system pool
  does not count).
- `orchestrator`: `orchestrator.enabled` is true.
- `dismissed`: the global setting `onboarding.dismissed` (bool, default `false`,
  admin-written), the only stored value, so dismissing holds across browsers.

## 3.3 When it opens

- It opens automatically for an admin when `account` or `worker` is false and
  `dismissed` is false. Closing it sets `onboarding.dismissed = true`.
- It can always be reopened from a **Set up** link on the Dashboard page, and from
  the empty state of `#chat`, which opens it directly at step 3.
- It is a modal over the current route. It works from 390 px to 1440 px.

## 3.4 Routing

- Home is renamed **Dashboard**: the route is `#dashboard`, and `#home` redirects
  there (old links keep working). Its content is unchanged ([home](home.spec.md)).
- The default route (empty or invalid hash) is `#chat` when
  `orchestrator.enabled` is true, and `#dashboard` otherwise.

# 4. Open Questions

- Whether step 2 should also offer to create a first project, since `create_task`
  needs one.
- Whether a "send a first task" step belongs here, or the chat covers it once the
  orchestrator is on.

# 5. Constraints

- No new table and no migration: one read route and one setting.
- UI copy is English. The wizard adds no account or pool logic of its own; a rule
  that changes in an account or worker spec changes here by reuse.

# 6. Implementation Consequences

Delivery order for this spec and [internal orchestrator](internal-orchestrator.spec.md):

1. **Spec PR**: both specs, the worker lifecycle 3.6 amendment and the index links.
2. **Onboarding and the rename** (this spec): `GET /api/onboarding`, the
   `onboarding.dismissed` setting, the wizard (steps 1–2; step 3 hidden until
   phase 3), `#dashboard` with the `#home` redirect. Tests: each fact, the
   auto-open rule, dismiss, the redirect, and that non-admin tokens get `403`.
3. **Internal orchestrator**: its spec 6, plus step 3 and the `#chat` default route.
4. **Skill improvements and bench journeys** (internal orchestrator 3.9); the
   internal section of the skill ships with phase 3.

## Verification boundary

- Commands: `make check`; the API tests for `/api/onboarding`; the dashboard
  Vitest suite for the wizard and routing.
- Verified: facts, auto-open, dismiss and routing at the unit level; the reused
  forms keep their own tests.
- Not verified: a newcomer's end-to-end run on a fresh node (account → worker →
  orchestrator) with real credentials; that needs a manual pass recorded in a
  report before onboarding can be called shipped.
