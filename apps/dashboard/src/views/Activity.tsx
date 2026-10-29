import { useEffect, useState } from "react";
import type { ArgusEvent, Task, TaskRead } from "../api";
import { relative } from "../format";
import { MilestoneLine, NoSignal } from "../milestones";
import { quietFacts } from "../stall";

/** The current time, re-read every `ms`, so "N s ago" keeps counting between events. */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(timer);
  }, [ms]);
  return now;
}

type Progress = Pick<
  Task,
  | "id"
  | "state"
  | "attempt"
  | "progress_message"
  | "progress_percent"
  | "progress_at"
  | "progress_kind"
  | "progress_data"
  | "lease_expires_at"
> &
  Pick<TaskRead, "quiet_at" | "last_signal_at" | "last_signal_source" | "stall_cause">;

/** Progress notes of the task's current attempt, newest first. */
export function attemptNotes(task: Progress, events: ArgusEvent[]): ArgusEvent[] {
  return events.filter(
    (e) =>
      e.kind === "task.progress" &&
      (e.data as { attempt?: number }).attempt === task.attempt &&
      e.subject_id === task.id,
  );
}

/** What the agent says it is doing now (live work spec 3.1). */
export function Activity({ task, events }: { task: Progress; events: ArgusEvent[] }) {
  const now = useNow();
  const notes = attemptNotes(task, events);
  const quietLine =
    task.state === "claimed" && task.quiet_at
      ? quietFacts(
          {
            age_s: Math.max(0, Math.floor((now - Date.parse(task.quiet_at)) / 1000)),
            last_signal_at: task.last_signal_at,
            last_signal_source: task.last_signal_source,
            stall_cause: task.stall_cause,
          },
          now,
        )
      : null;
  if (task.state !== "claimed" && notes.length === 0 && !quietLine) return null;
  return (
    <div className="activity">
      {quietLine && (
        <p className="quiet-claim" role="status">
          {quietLine.charAt(0).toUpperCase() + quietLine.slice(1)}
        </p>
      )}
      <div className="progress">
        {task.progress_percent != null && (
          <span className="bar">
            <span style={{ width: `${task.progress_percent}%` }} />
          </span>
        )}
        <NoSignal task={task} now={now} />
        <MilestoneLine task={task} now={now} />
        <span>{task.progress_message ?? "No progress note yet."}</span>
        <span className="muted small">
          {task.progress_at ? `updated ${relative(task.progress_at, now)}` : "silent so far"}
          {task.state === "claimed" && ` · lease ${relative(task.lease_expires_at, now)}`}
        </span>
      </div>
      {notes.length > 1 && (
        <ol className="notes">
          {notes.slice(1).map((e) => (
            <li key={e.id}>
              <time dateTime={e.at} title={e.at} className="muted small">
                {relative(e.at, now)}
              </time>{" "}
              {(e.data as { message?: string }).message}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
