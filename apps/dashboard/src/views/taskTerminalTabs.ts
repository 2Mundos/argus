import type { LogLine } from "../taskLog";

export type TaskTerminalTab = "all" | "worker" | "delegate" | "changes";

/** Tabs that would not show only "No output yet" / an empty changes state. */
export function taskTerminalVisibleTabs(
  log: { ready: boolean; lines: LogLine[] },
  showChanges: boolean,
  hasChangesSnapshot: boolean,
): TaskTerminalTab[] {
  const tabs: TaskTerminalTab[] = [];
  if (!log.ready) {
    tabs.push("all");
    return tabs;
  }
  const hasWorker = log.lines.some((l) => l.source === "worker");
  const hasDelegate = log.lines.some((l) => l.source === "delegate");
  if (hasWorker && hasDelegate) tabs.push("all");
  if (hasWorker) tabs.push("worker");
  if (hasDelegate) tabs.push("delegate");
  if (showChanges && hasChangesSnapshot) tabs.push("changes");
  return tabs;
}
