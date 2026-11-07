import type { TaskState } from "./api";

/**
 * The Tasks view's URL-backed filter: `states` and the submitted literal search `q`
 * ("" for none), e.g. `#tasks?state=failed&q=retry`.
 */
export type TaskFilter = { states: TaskState[]; q: string };

export const NO_TASK_FILTER: TaskFilter = { states: [], q: "" };

const TASK_STATES: readonly TaskState[] = [
  "awaiting_approval",
  "pending",
  "ready",
  "claimed",
  "blocked",
  "in_review",
  "succeeded",
  "failed",
  "cancelled",
];
const MAX_QUERY = 256;

/** The hash path without its `?query`, e.g. `tasks/abc` for `#tasks/abc?state=failed`. */
export function hashPath(hash: string): string {
  return hash.replace(/^#/, "").split("?")[0] ?? "";
}

/**
 * The filter in a hash's query (`#tasks?state=blocked&state=failed&q=retry`). Oversized,
 * unknown or malformed states give no state filter, never a partial guess; `q` is
 * free text and the server judges whether it is too broad.
 */
export function parseTaskFilter(hash: string): TaskFilter {
  const at = hash.indexOf("?");
  if (at < 0) return NO_TASK_FILTER;
  const params = new URLSearchParams(hash.slice(at + 1));
  const q = params.get("q")?.trim() ?? "";
  params.delete("q");
  const none = { ...NO_TASK_FILTER, q };
  if (params.toString().length > MAX_QUERY) return none;
  const values = params.getAll("state");
  if (values.length > TASK_STATES.length) return none;
  const states = new Set<TaskState>();
  for (const value of values) {
    const state = TASK_STATES.find((s) => s === value);
    if (!state) return none;
    states.add(state);
  }
  return { states: TASK_STATES.filter((s) => states.has(s)), q };
}

/** The hash for the Tasks board with `filter` applied; bare `#tasks` when it is empty. */
export function tasksHash(filter: Partial<TaskFilter>): string {
  const query = new URLSearchParams((filter.states ?? []).map((s) => ["state", s]));
  const q = filter.q?.trim();
  if (q) query.set("q", q);
  return query.size ? `#tasks?${query}` : "#tasks";
}
