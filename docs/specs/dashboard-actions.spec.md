# Dashboard action feedback

# 1. Purpose and Scope

Every dashboard control that awaits a server mutation must show pending
feedback while the request is in flight: the control disables, reports
`aria-busy`, and shows an in-button spinner so a click feels acknowledged
and cannot double-fire.

Shared primitives live in the dashboard app (`ActionButton`, `useAction`,
`Spinner`). Views do not invent local `busy` / `saving` flags for the same
job. Pure UI controls (tabs, expand/collapse, open a dialog, client-only
filters) stay plain `<button>`s.

# 2. Non-Goals

- Not a full button design system or variant library.
- No toast or success-flash system.
- No change to read-only loading (`useLiveData`, page "Loading…" copy).
- No new dependency (no React Query / mutation library).

# 3. Decisions Made

## 3.1 Two primitives

- **`useAction()`** — pending state for forms and multi-button toolbars.
  Exposes `busy`, `pending` (a string key or `true`), `is(key)`, and
  `run(fn, key?)`. A second `run` while busy is ignored (single-flight).
  The caller still owns try/catch and error/notice UI.
- **`ActionButton`** — drop-in `<button>` that either owns busy via an
  `action` prop, or takes controlled `pending` from a parent `useAction`.
  While pending: `disabled`, `aria-busy="true"`, class `is-pending`, spinner
  left of the label. Optional `pendingLabel` replaces the idle label for
  long verbs; default keeps `children`. Existing classes (`primary`,
  `danger`, `link`) are unchanged.

## 3.2 Visual

- Reuse the existing `.spinner` ring; size it to the button (`0.85em`).
- Lock button width while pending so toolbars do not jump.
- `cursor: wait` and a slight opacity dip; honor `prefers-reduced-motion`.
- Idle button chrome is unchanged.

## 3.3 When it applies

A control that calls `call(api.POST|PUT|PATCH|DELETE…)` (or an equivalent
write that returns a Promise the UI awaits) uses `ActionButton` /
`useAction`. Opening a local dialog, switching tabs, or toggling client
state does not.

In a toolbar of several mutations, one `useAction` keys the active action:
only the clicked button shows the spinner; siblings disable via
`disabled={action.busy}`.

# 4. Open Questions

- Whether a shared form-level error banner should sit beside `useAction`
  later; today each view still owns its error string.

# 5. Constraints

- Dashboard only; English UI copy; TypeScript strict; Biome format.
- No sleeps or polling in tests; hold a deferred promise and resolve it.
- Single-node stack unchanged; no API or schema change.

# 6. Implementation Consequences

- `apps/dashboard/src/ActionButton.tsx` (`ActionButton`, `useAction`,
  `Spinner`) and pending styles in `styles.css`.
- Mutation sites across views migrate onto the primitive; ad-hoc
  `setBusy` / `setSaving` for the same job are removed where the primitive
  covers them.
- Unit tests for single-flight, keyed pending, and unmount mid-flight.
