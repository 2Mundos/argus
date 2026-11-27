import { type ReactNode, useEffect, useState } from "react";
import type { ArgusEvent, Task, TaskRead } from "../api";
import { DateChip } from "../DateChip";
import { MilestoneLine, NoSignal, ProgressPie } from "../milestones";
import { finalizationLine, quietFacts } from "../stall";
import { TaskStateBadge } from "../TaskStateBadge";

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

function noteMessage(event: ArgusEvent): string {
  return (event.data as { message?: string }).message ?? "";
}

/** One-line progress note: time chip + clamped message. */
function NoteRow({ event, now }: { event: ArgusEvent; now: number }) {
  const message = noteMessage(event);
  return (
    <li>
      <DateChip at={event.at} now={now} />{" "}
      <span className="note-text" title={message || undefined}>
        {message}
      </span>
    </li>
  );
}

function NotesList({
  notes,
  now,
  collapseAfter = 0,
}: {
  notes: ArgusEvent[];
  now: number;
  /** When > 0 and notes exceed this, wrap the rest in “Earlier notes”. */
  collapseAfter?: number;
}) {
  if (notes.length === 0) return null;
  const visible = collapseAfter > 0 ? notes.slice(0, collapseAfter) : notes;
  const earlier = collapseAfter > 0 ? notes.slice(collapseAfter) : [];
  return (
    <>
      {visible.length > 0 && (
        <ol className="notes">
          {visible.map((e) => (
            <NoteRow key={e.id} event={e} now={now} />
          ))}
        </ol>
      )}
      {earlier.length > 0 && (
        <details className="activity-notes">
          <summary>Earlier notes ({earlier.length})</summary>
          <ol className="notes">
            {earlier.map((e) => (
              <NoteRow key={e.id} event={e} now={now} />
            ))}
          </ol>
        </details>
      )}
    </>
  );
}

function LeaseChip({ at, now }: { at: string | null | undefined; now: number }) {
  return (
    <span className="badge meta-lease" title="lease expires">
      <svg viewBox="0 0 16 16" aria-hidden="true">
        <circle cx="8" cy="8" r="5.2" fill="none" stroke="currentColor" strokeWidth="1.5" />
        <path
          d="M8 5v3.2l2 1.3"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
        />
      </svg>
      <DateChip at={at} now={now} />
    </span>
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
          <NotesList notes={prior} now={now} collapseAfter={3} />
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
  // Claimed tasks keep a progress row (lease / quiet); finished ones only with notes.
  const showProgress = task.state === "claimed" || hasProgress;
  if (task.state !== "claimed" && notes.length === 0 && !quietLine && !finalLine) return null;

  // Milestone already carries the live status; otherwise show the progress message.
  const primary: ReactNode = task.progress_kind ? (
    <MilestoneLine task={task} now={now} />
  ) : task.progress_message || task.state === "claimed" ? (
    <span className="activity-primary">{task.progress_message ?? "No progress note yet."}</span>
  ) : null;

  // notes[0] mirrors the status strip; only older ones go in the disclosure.
  const earlier = notes.slice(1);

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
        <div className="activity-strip">
          <div className="activity-strip-main">
            {task.progress_percent != null && <ProgressPie percent={task.progress_percent} />}
            {task.state === "claimed" && <TaskStateBadge state={task.state} />}
            <div className="activity-strip-text">
              <NoSignal task={task} now={now} />
              {primary}
            </div>
          </div>
          <div className="activity-strip-meta muted small">
            {task.progress_at ? (
              <span title="last progress update">
                <DateChip at={task.progress_at} now={now} />
              </span>
            ) : task.state === "claimed" ? (
              <span>silent so far</span>
            ) : null}
            {task.state === "claimed" && <LeaseChip at={task.lease_expires_at} now={now} />}
          </div>
        </div>
      )}
      {earlier.length > 0 && (
        <details className="activity-notes">
          <summary>Earlier notes ({earlier.length})</summary>
          <ol className="notes">
            {earlier.map((e) => (
              <NoteRow key={e.id} event={e} now={now} />
            ))}
          </ol>
        </details>
      )}
    </div>
  );
}
