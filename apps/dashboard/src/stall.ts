import { relative } from "./format";

/** What the worker host last saw for a quiet claim (stall visibility 3.7). */
export const STALL_CAUSE_COPY: Record<string, string> = {
  workspace_missing: "workspace missing on the host",
  session_gone: "session container gone",
  silent_delegate_active: "delegate busy but silent",
  no_progress: "session running, no progress",
  paused_disk: "paused by the disk guard (low disk)",
};

export function dur(s: number): string {
  return s < 60
    ? `${s}s`
    : s < 3600
      ? `${Math.floor(s / 60)}m`
      : s < 86_400
        ? `${Math.floor(s / 3600)}h`
        : `${Math.floor(s / 86_400)}d`;
}

/** Lower-case quiet sentence tail (Inbox card and Activity panel). */
export function quietFacts(
  q: {
    age_s: number;
    last_signal_at?: string | null;
    last_signal_source?: string | null;
    stall_cause?: string | null;
  },
  now?: number,
): string {
  const signalLine =
    q.last_signal_at && q.last_signal_source
      ? `; last signal: ${q.last_signal_source} at ${relative(q.last_signal_at, now)}`
      : q.last_signal_at
        ? `; last signal at ${relative(q.last_signal_at, now)}`
        : "";
  const cause =
    q.stall_cause && q.stall_cause !== "unknown"
      ? `; likely cause: ${STALL_CAUSE_COPY[q.stall_cause]}`
      : "";
  return `quiet for ${dur(q.age_s)}${signalLine}${cause}.`;
}
