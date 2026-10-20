import type { Task } from "./api";
import { relative, shortId } from "./format";

// Progress milestones (docs/specs/progress-milestones.spec.md 3.4).

/** Minutes without any progress signal after which a claimed task is shown as quiet. */
export function parseNoSignalMinutes(raw: string | undefined): number {
  const n = Number(raw);
  return raw?.trim() && Number.isFinite(n) && n > 0 ? n : 10;
}
export const NO_SIGNAL_MINUTES = parseNoSignalMinutes(import.meta.env.VITE_NO_SIGNAL_MINUTES);

const LABELS: Record<string, string> = {
  commit_created: "Commit created",
  pr_opened: "PR opened",
  ci_running: "CI running",
  ci_passed: "CI passed",
  ci_failed: "CI failed",
  gate_failed: "Gate failed",
  waiting_input: "Waiting for input",
};

export type Signal = Pick<
  Task,
  | "state"
  | "progress_message"
  | "progress_percent"
  | "progress_at"
  | "progress_kind"
  | "progress_data"
>;

/** Whole minutes of silence on a claimed task, or null while it is within `threshold`. */
export function noSignalMinutes(
  task: Pick<Task, "state" | "progress_at">,
  now: number,
  threshold = NO_SIGNAL_MINUTES,
): number | null {
  if (task.state !== "claimed" || !task.progress_at) return null;
  const minutes = (now - new Date(task.progress_at).getTime()) / 60_000;
  return minutes > threshold ? Math.floor(minutes) : null;
}

const text = (v: unknown): string | null => (typeof v === "string" && v ? v : null);

/** Milestone label, short SHA, PR link and when it happened; nothing for a task without a kind. */
export function MilestoneLine({ task, now }: { task: Signal; now: number }) {
  if (!task.progress_kind) return null;
  const data = (task.progress_data ?? {}) as Record<string, unknown>;
  const sha = text(data.sha);
  const url = text(data.url);
  const number = typeof data.number === "number" ? data.number : null;
  const at = text(data.at) ?? task.progress_at;
  const link = url && /^https?:\/\//i.test(url) ? url : null;
  return (
    <span className="milestone small">
      <strong>{LABELS[task.progress_kind] ?? task.progress_kind}</strong>
      {sha && <code> {shortId(sha).slice(0, 7)}</code>}
      {text(data.gate) && <> · {text(data.gate)}</>}
      {link && number != null && (
        <>
          {" "}
          <a
            href={link}
            target="_blank"
            rel="noopener noreferrer"
            // The card is a button: keep the click from opening the drawer, and open the
            // link ourselves because browsers differ on anchors inside buttons.
            onClick={(e) => {
              e.stopPropagation();
              e.preventDefault();
              window.open(link, "_blank", "noopener,noreferrer");
            }}
          >
            #{number}
          </a>
        </>
      )}
      {at && <span className="muted"> · {relative(at, now)}</span>}
    </span>
  );
}

/** The quiet-agent warning for a claimed task, or nothing. */
export function NoSignal({ task, now }: { task: Signal; now: number }) {
  const minutes = noSignalMinutes(task, now);
  if (minutes == null) return null;
  return (
    <span className="no-signal small" role="status">
      No signal for {minutes} min
    </span>
  );
}

/**
 * What a claimed task card shows for progress: the milestone (if any), a note newer than
 * it, and the no-signal warning, with the last note marked as stale when quiet.
 */
export function TaskSignal({ task, now }: { task: Signal; now: number }) {
  const quiet = noSignalMinutes(task, now) != null;
  const at = (task.progress_data as { at?: string } | null)?.at;
  const newerNote =
    !task.progress_kind ||
    (at && task.progress_at && Date.parse(task.progress_at) - Date.parse(at) > 1000);
  const note = task.progress_message && newerNote;
  return (
    <>
      <NoSignal task={task} now={now} />
      <MilestoneLine task={task} now={now} />
      {note && (
        <span className={quiet ? "progress stale" : "progress"}>
          {task.progress_percent != null && (
            <span className="bar">
              <span style={{ width: `${task.progress_percent}%` }} />
            </span>
          )}
          <span className="small">
            {quiet && "Last note: "}
            {task.progress_message}
          </span>
        </span>
      )}
    </>
  );
}
