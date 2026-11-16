import type { TaskRead } from "./api";

// The merge and review decisions the claim and the PR gate will apply, read with the task
// (docs/specs/worker-lifecycle.spec.md 3.7). Display only: nothing here merges or approves.

type Merge = TaskRead["merge"];
type Gate = NonNullable<TaskRead["pr_gate"]>;

const GATE_REASONS: Record<string, string> = {
  ci_running: "checks running",
  ci_failed: "checks failed",
  ci_unknown: "checks unknown",
  ci_missing: "no checks",
  not_mergeable: "not mergeable",
};

export function mergeLine(merge: Merge): string {
  if (merge.granted_by === "auto_merge") {
    const by = merge.auto_merge_source === "project" ? "project" : "global";
    const line = `authorized by ${by} auto-merge (deliverable ${merge.deliverable})`;
    return merge.may_merge ? line : `${line}, but the task has no repository:write`;
  }
  if (merge.granted_by === "allow_merge") {
    const line = "authorized by allow_merge in the task input";
    return merge.may_merge ? line : `${line}, but the task has no repository:write`;
  }
  return `not authorized — deliverable ${merge.deliverable}; enable auto-merge (with pr_merged) or allow_merge`;
}

export function gateReason(reason: string, gate: Gate): string {
  if (reason === "review_required")
    return `review required, ${gate.approvals_for_head ?? 0} approvals`;
  return GATE_REASONS[reason] ?? reason;
}

export function TaskReadiness({ task }: { task: TaskRead }) {
  // An older API answers without the decisions: show nothing rather than guess.
  if (!task.merge) return null;
  const gate = task.pr_gate;
  return (
    <section className="delivery" aria-label="Merge and review">
      <div>Merge: {mergeLine(task.merge)}</div>
      <div>
        Review: {task.forge_review === "required" ? "required by forge review policy" : "off"}
      </div>
      {gate && (
        <div>
          PR gate{gate.head_sha ? ` at ${gate.head_sha.slice(0, 7)}` : ""}:{" "}
          {gate.pr_state !== "open"
            ? `PR ${gate.pr_state}`
            : gate.ready
              ? "ready"
              : gate.reasons.map((r) => gateReason(r, gate)).join("; ")}
        </div>
      )}
    </section>
  );
}
