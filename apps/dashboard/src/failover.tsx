import type { Task } from "./api";
import { DateChip } from "./DateChip";
import { relative } from "./format";

// Account failover on a task (docs/specs/account-selection.spec.md 3.6).

type FailoverInfo = Pick<
  Task,
  "state" | "block_reason" | "refill_at" | "serving_account" | "failover"
> &
  Partial<Pick<Task, "failure_class">>;

// What stopped the run, in words an operator can tell apart.
const CODES: Record<string, string> = {
  quota_exhausted: "Quota exhausted",
  auth_expired: "Login expired",
  network_unavailable: "Network unavailable",
  runtime_switch_failed: "Runtime switch failed",
  implementation: "Implementation error",
};

const label = (code: string | null | undefined) => (code ? (CODES[code] ?? code) : null);

/** The provider's reset: the stated time, else its own words, else that it said nothing. */
function resetNote(f: NonNullable<Task["failover"]>): string | null {
  // Stated reset times use DateChip at the call site when rendered alone; here we only
  // keep non-date prose for the compact reason line.
  if (f.reset_status === "stated" && f.reset_at) return `resets ${relative(f.reset_at)}`;
  if (f.reset_status === "ambiguous")
    return f.reset_text ? `reset (provider's words, no zone): ${f.reset_text}` : "reset ambiguous";
  if (f.reset_status === "absent") return "no reset stated";
  return null;
}

/** Serving account, the last switch with Argus's literal reason, and when quota is back. */
export function TaskFailover({ task, now = Date.now() }: { task: FailoverInfo; now?: number }) {
  const f = task.failover;
  const waiting = task.state === "blocked" && task.block_reason === "awaiting_quota";
  const cls = task.failure_class && !f ? label(task.failure_class) : null;
  if (!task.serving_account && !f && !waiting && !cls) return null;
  return (
    <>
      {task.serving_account && (
        <>
          <dt>Account</dt>
          <dd>
            {task.serving_account.name}{" "}
            <span className="muted">({task.serving_account.provider})</span>
            {task.serving_account.model && (
              <span className="muted">
                {" "}
                · {task.serving_account.model}
                {task.serving_account.host && ` @ ${task.serving_account.host}`}
              </span>
            )}
          </dd>
        </>
      )}
      {waiting && (
        <>
          <dt>Waiting for quota</dt>
          <dd>
            {task.refill_at ? (
              <>
                until <DateChip at={task.refill_at} now={now} />
              </>
            ) : (
              "until an account recovers"
            )}
          </dd>
        </>
      )}
      {cls && (
        <>
          <dt>Failure</dt>
          <dd>{cls}</dd>
        </>
      )}
      {f && (
        <>
          <dt>Last failover</dt>
          <dd>
            {f.from_account ?? "—"} → {f.to_account ?? "no account left"}{" "}
            <span className="muted">
              at attempt {f.attempt}, <DateChip at={f.at} now={now} />
            </span>
            <div className="small muted">{f.reason}</div>
            <div className="small muted">
              {[label(f.reason_code), resetNote(f), f.source && `via ${f.source}`]
                .filter(Boolean)
                .join(" · ")}
            </div>
            {f.outcome === "switch_failed" || f.apply === "failed" ? (
              <div className="small">
                Runtime switch failed{f.apply_error ? `: ${f.apply_error}` : ""}
              </div>
            ) : f.apply === "applied" ? (
              <div className="small muted">
                Applied {f.applied_at ? <DateChip at={f.applied_at} now={now} /> : null}
              </div>
            ) : null}
          </dd>
        </>
      )}
    </>
  );
}
