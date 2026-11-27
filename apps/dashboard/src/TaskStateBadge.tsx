import type { TaskState } from "./api";

const LABEL: Record<TaskState, string> = {
  awaiting_approval: "awaiting approval",
  pending: "pending",
  ready: "ready",
  claimed: "in progress",
  blocked: "blocked",
  in_review: "in review",
  succeeded: "succeeded",
  failed: "failed",
  cancelled: "cancelled",
};

export function stateLabel(state: TaskState): string {
  return LABEL[state];
}

/** Play, check, ban — one path each, 24 viewBox. */
const ICON: Partial<Record<TaskState, string>> = {
  claimed: "M8 5.5v13l11-6.5Z",
  succeeded: "M9.5 16.2 5.3 12l-1.4 1.4 5.6 5.6 11-11-1.4-1.4Z",
  cancelled:
    "M12 2a10 10 0 1 1 0 20 10 10 0 0 1 0-20Zm0 2a8 8 0 0 0-6.3 12.9L16.9 5.7A8 8 0 0 0 12 4Zm6.3 3.1L7.1 18.3A8 8 0 0 0 18.3 7.1Z",
};

/** Task state chip: claimed / succeeded / cancelled are icons with a tooltip. */
export function TaskStateBadge({ state }: { state: TaskState }) {
  const label = LABEL[state];
  const path = ICON[state];
  if (path) {
    return (
      <span className={`badge state-icon-badge state-${state}`} title={label} aria-label={label}>
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d={path} fill="currentColor" />
        </svg>
      </span>
    );
  }
  return <span className={`badge state-${state}`}>{label}</span>;
}
