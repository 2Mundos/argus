import type { TaskState } from "./api";

/** The states an operator can archive; the API refuses any other. */
export const ARCHIVABLE: TaskState[] = ["succeeded", "failed", "cancelled"];

export interface ArchiveCriteria {
  states: TaskState[];
  /** Empty: every project. */
  project: string;
  /** Empty: any age. */
  olderThanDays: string;
}

interface BulkArchiveBody {
  states: TaskState[];
  project?: string;
  older_than_days?: number;
  dry_run: boolean;
}

/** The request for these criteria, or null while they select nothing. */
export function bulkBody(c: ArchiveCriteria, dryRun: boolean): BulkArchiveBody | null {
  const states = ARCHIVABLE.filter((s) => c.states.includes(s));
  const days = c.olderThanDays.trim() === "" ? undefined : Number(c.olderThanDays);
  if (states.length === 0) return null;
  if (days !== undefined && (!Number.isInteger(days) || days < 0)) return null;
  return {
    states,
    ...(c.project ? { project: c.project } : {}),
    ...(days !== undefined ? { older_than_days: days } : {}),
    dry_run: dryRun,
  };
}

/** The confirm button's label: it always names how many tasks it will archive. */
export function confirmLabel(count: number | undefined): string {
  if (count === undefined) return "Counting…";
  if (count === 0) return "Nothing to archive";
  return `Archive ${count} ${count === 1 ? "task" : "tasks"}`;
}
