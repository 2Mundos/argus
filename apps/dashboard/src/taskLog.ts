import type { ArgusEvent } from "./api";

// Live task log (docs/specs/task-live-log.spec.md 3.6).

export type LogSource = "worker" | "delegate";
export type LogFilter = "all" | LogSource;

export interface LogLine {
  seq: number;
  source: LogSource;
  at: string;
  line: string;
}

/** Lines kept in the panel; the server ring holds 2000 per attempt. */
export const MAX_LINES = 2000;

interface LogData {
  task_id: string;
  attempt: number;
  source: LogSource;
  seq: number;
  at: string;
  lines: string[];
}

/** The lines of a `task.log` event for `taskId`, or null for any other event. */
export function logEvent(
  event: Pick<ArgusEvent, "kind" | "data">,
  taskId: string,
): { attempt: number; lines: LogLine[] } | null {
  if (event.kind !== "task.log") return null;
  const d = event.data as unknown as LogData;
  if (d.task_id !== taskId) return null;
  return {
    attempt: d.attempt,
    lines: d.lines.map((line, i) => ({ seq: d.seq + i, source: d.source, at: d.at, line })),
  };
}

/** Merge new lines into the list by `seq` (backfill and live overlap), oldest first, capped. */
export function mergeLines(have: LogLine[], more: LogLine[]): LogLine[] {
  const bySeq = new Map(have.map((l) => [l.seq, l]));
  for (const l of more) bySeq.set(l.seq, l);
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq).slice(-MAX_LINES);
}

export function filterLines(lines: LogLine[], filter: LogFilter): LogLine[] {
  return filter === "all" ? lines : lines.filter((l) => l.source === filter);
}
