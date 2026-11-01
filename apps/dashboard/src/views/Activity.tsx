import { useEffect, useState } from "react";
import type { ArgusEvent, Task, TaskRead } from "../api";
import { DateChip } from "../DateChip";
import { MilestoneLine, NoSignal } from "../milestones";
import { finalizationLine, quietFacts } from "../stall";

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
  Pick<TaskRead, "quiet_at" | "last_signal_at" | "last_signal_source" | "stall_cause"> & {
    finalization?: TaskRead["finalization"];
  };

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
export function Activity({
  task,
  events,
  selected = task.attempt,
}: {
  task: Progress;
  events: ArgusEvent[];
  /** The attempt chosen in the attempt strip; a prior one shows only its own notes. */
  selected?: number;
}) {
  const now = useNow();
  if (selected !== task.attempt) {
    const prior = attemptNotes({ ...task, attempt: selected }, events);
    return (
      <div className="activity">
        {prior.length === 0 ? (
          <span className="muted small">No progress notes for this attempt.</span>
        ) : (
          <ol className="notes">
            {prior.map((e) => (
              <li key={e.id}>
                <DateChip at={e.at} now={now} /> {(e.data as { message?: string }).message}
              </li>
            ))}
          </ol>
        )}
      </div>
    );
  }
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
  const finalLine = task.finalization ? finalizationLine(task.finalization, now) : null;
  const hasProgress =
    task.progress_message != null ||
    task.progress_percent != null ||
    task.progress_at != null ||
    task.progress_kind != null;
  // Claimed tasks keep a progress row (lease / quiet); finished ones only when there is a note.
  const showProgress = task.state === "claimed" || hasProgress;
  if (task.state !== "claimed" && notes.length === 0 && !quietLine && !finalLine && !hasProgress)
    return null;
  return (
    <div className="activity">
      {finalLine && (
        <p className="finalization muted small" data-step={task.finalization?.step}>
          {finalLine}
        </p>
      )}
      {quietLine && (
        <p className="quiet-claim" role="status">
          {quietLine.charAt(0).toUpperCase() + quietLine.slice(1)}
        </p>
      )}
      {showProgress && (
        <div className="progress">
          {task.progress_percent != null && (
            <span className="bar">
              <span style={{ width: `${task.progress_percent}%` }} />
            </span>
          )}
          <NoSignal task={task} now={now} />
          <MilestoneLine task={task} now={now} />
          {(task.progress_message || task.state === "claimed") && (
            <span>{task.progress_message ?? "No progress note yet."}</span>
          )}
          <span className="muted small">
            {task.progress_at ? (
              <>
                updated <DateChip at={task.progress_at} now={now} />
              </>
            ) : task.state === "claimed" ? (
              "silent so far"
            ) : null}
            {task.state === "claimed" && (
              <>
                {" · lease "}
                <DateChip at={task.lease_expires_at} now={now} />
              </>
            )}
          </span>
        </div>
      )}
      {notes.length > 1 && (
        <ol className="notes">
          {notes.slice(1).map((e) => (
            <li key={e.id}>
              <DateChip at={e.at} now={now} /> {(e.data as { message?: string }).message}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
