import type { TaskState } from "./api";

/** The Tasks view's URL-backed filter. `q` (literal search) joins `states` here. */
export type TaskFilter = { states: TaskState[] };

export const NO_TASK_FILTER: TaskFilter = { states: [] };

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
 * The filter in a hash's query (`#tasks?state=blocked&state=failed`). Anything
 * oversized, unknown or malformed gives the unfiltered board, never a partial guess.
 */
export function parseTaskFilter(hash: string): TaskFilter {
  const at = hash.indexOf("?");
  if (at < 0) return NO_TASK_FILTER;
  const query = hash.slice(at + 1);
  if (query.length > MAX_QUERY) return NO_TASK_FILTER;
  const values = new URLSearchParams(query).getAll("state");
  if (values.length > TASK_STATES.length) return NO_TASK_FILTER;
  const states = new Set<TaskState>();
  for (const value of values) {
    const state = TASK_STATES.find((s) => s === value);
    if (!state) return NO_TASK_FILTER;
    states.add(state);
  }
  return { states: TASK_STATES.filter((s) => states.has(s)) };
}

/** The hash for the Tasks board with `filter` applied; bare `#tasks` when it is empty. */
export function tasksHash(filter: TaskFilter): string {
  if (filter.states.length === 0) return "#tasks";
  const query = new URLSearchParams(filter.states.map((s) => ["state", s]));
  return `#tasks?${query}`;
}
