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

/** The rows the panel shows: up to the pause cutoff, then the source filter. */
export function visibleLines(
  lines: LogLine[],
  filter: LogFilter,
  pausedAt: number | null,
): LogLine[] {
  return filterLines(pausedAt == null ? lines : lines.filter((l) => l.seq <= pausedAt), filter);
}

/** A row as shown on screen. */
export function lineText(l: LogLine): string {
  return `${l.source === "delegate" ? "[delegate] " : ""}${l.line}`;
}

/** The download of the visible rows: a plain text excerpt, built from the rows shown only. */
export function logDownload(
  taskId: string,
  attempt: number,
  filter: LogFilter,
  rows: LogLine[],
  pausedAt: number | null,
): { filename: string; text: string } {
  const id = taskId.replace(/[^A-Za-z0-9_-]/g, "-").slice(0, 64);
  const header = [
    "# Argus task log: loaded excerpt, not a full transcript",
    `# task: ${taskId}`,
    `# attempt: ${attempt}`,
    `# source: ${filter}`,
    `# cutoff: ${pausedAt == null ? "live" : `paused after seq ${pausedAt}`}`,
    `# lines: ${rows.length}`,
  ];
  return {
    filename: `argus-task-${id}-attempt-${attempt}-${filter}.txt`,
    text: `${header.join("\n")}\n\n${rows.map((l) => `${lineText(l)}\n`).join("")}`,
  };
}
