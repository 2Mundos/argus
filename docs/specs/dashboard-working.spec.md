---
status: implemented
created: 2026-11-04
updated: 2026-11-04
---

# Dashboard working indicator

# 1. Purpose and Scope

Show, in one place everyone looks at, that Argus is doing something for you. The
gradient of the **ARGUS** wordmark rolls while the dashboard is working: a mutation
is in flight, or the orchestrator is answering a chat message. The same rolling
gradient marks the inline "working" line in the chat thread.

# 2. Non-Goals

- No change to read-only loading: `useLiveData`, "Loading…" copy and the live
  refresh of already-shown data do not make the wordmark roll
  ([dashboard actions](dashboard-actions.spec.md)).
- No progress or percentage, no sound, no new dependency.
- Not a replacement for the in-button spinner; the control that was clicked keeps it.

# 3. Decisions Made

- **One shared signal.** A module-level count of active "working" claims
  (`working.ts`): `startWorking()` returns an idempotent release, `useWorking()`
  reads whether any claim is held, `useWorkingWhile(active)` holds one while a
  component says so. The count lives outside React so any view can claim it.
- **Who claims it.** `ActionButton` while it is pending (so every mutation using
  the [action primitives](dashboard-actions.spec.md) counts, controlled or not), and
  the chat thread while its latest message is the admin's, not yet answered and
  younger than the 15-minute turn timeout. A message the orchestrator has not
  picked up (`queued`) counts only while the orchestrator is registered; one it is
  working on (`delivered`) always counts. A stale or abandoned one never does.
- **The wordmark.** `Brand` rolls while `useWorking()` is true and stops at the end
  of the current cycle (`animationiteration`), so the gradient never snaps back
  mid-roll. The rolling gradient is a seamless tile; at rest the wordmark shows the
  same colours.
- **Inline.** `Working` is the rolling text with a label ("Working…", "Waiting for
  the orchestrator…"); mounted, it claims the signal itself. The chat shows it under
  the thread while a reply is pending, as `role="status"`.
- **Motion preference.** With `prefers-reduced-motion: reduce` nothing animates;
  the label and the in-button spinner still say the work is happening.

# 4. Open Questions

Whether initial page loads should also make the wordmark roll. They do not today
(2): a data load that resolves to nothing, or a refetch on every event, would keep
it moving without telling anyone anything.

# 5. Constraints

Dashboard only; no API, schema or wire change. All engines behave the same.
English copy. No sleeps or polling in tests: drive the signal directly and fire
the animation event.

# 6. Implementation Consequences

`apps/dashboard/src/working.ts` and `Working.tsx`; `Brand.tsx`; `ActionButton.tsx`
claims while pending; `views/Chat.tsx` shows `Working`; shared `.roll-text` /
`.rolling` styles in `styles.css`.

## Verification boundary

- Commands: `pnpm --filter @argus/dashboard test`,
  `pnpm --filter @argus/dashboard typecheck`, `python3 scripts/validate_docs.py`.
- Verified in a browser: the wordmark rolls while a chat reply is pending and while a
  mutation runs, and stops at a cycle boundary.
- Not verified: behaviour with a real orchestrator turn.
