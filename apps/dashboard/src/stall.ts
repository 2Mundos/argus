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

type FinalizationRead = {
  step: "executing" | "finalizing_checkpoint" | "finished" | "unknown";
  execution_s: number | null;
  checkpoint: {
    state: "running" | "saved" | "nothing_to_save" | "failed" | "unknown";
    started_at: string | null;
    duration_s: number | null;
    detail: string | null;
  } | null;
};

/** Execution versus follow-up of the newest attempt (task finalization 3.5). A
 *  finished agent says nothing about preservation: no checkpoint report, no sentence. */
export function finalizationLine(f: FinalizationRead, now?: number): string {
  const took = (s: number | null) => (s == null ? "" : ` (took ${dur(s)})`);
  if (f.step === "executing") return "Agent running.";
  const agent = `Agent finished${took(f.execution_s)}.`;
  const c = f.checkpoint;
  if (!c) return agent;
  switch (c.state) {
    case "running":
      return `${agent} Saving checkpoint…`;
    case "saved":
      return `${agent} Checkpoint saved${took(c.duration_s)}.`;
    case "nothing_to_save":
      return `${agent} Nothing to checkpoint.`;
    case "failed":
      return `${agent} Checkpoint failed${c.detail ? `: ${c.detail}` : ""}${took(c.duration_s)}.`;
    case "unknown":
      return `${agent} Checkpoint state unknown: worker silent since ${c.started_at ? relative(c.started_at, now) : "start"}.`;
  }
}
