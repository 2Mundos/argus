import type { Task } from "./api";
import { relative } from "./format";

// Account failover on a task (docs/specs/account-selection.spec.md 3.6).

export type FailoverInfo = Pick<
  Task,
  "state" | "block_reason" | "refill_at" | "serving_account" | "failover"
>;

/** Serving account, the last switch with Argus's literal reason, and when quota is back. */
export function TaskFailover({ task, now = Date.now() }: { task: FailoverInfo; now?: number }) {
  const f = task.failover;
  const waiting = task.state === "blocked" && task.block_reason === "awaiting_quota";
  if (!task.serving_account && !f && !waiting) return null;
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
            {task.refill_at
              ? `until ${task.refill_at} (${relative(task.refill_at, now)})`
              : "until an account recovers"}
          </dd>
        </>
      )}
      {f && (
        <>
          <dt>Last failover</dt>
          <dd>
            {f.from_account ?? "—"} → {f.to_account ?? "no account left"}{" "}
            <span className="muted">
              at attempt {f.attempt}, {relative(f.at, now)}
            </span>
            <div className="small muted">{f.reason}</div>
          </dd>
        </>
      )}
    </>
  );
}
