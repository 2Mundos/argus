import { useState } from "react";
import { ActionButton, useAction } from "./ActionButton";
import { api, call, type Delivery } from "./api";
import { DateChip } from "./DateChip";

// Delivery of a task's pull request (docs/specs/task-delivery.spec.md 3.3–3.5).

const BLOCKED: Record<string, string> = {
  not_in_review: "Accepting is available once the task is in review.",
  checklist_pending: "Checklist items are still pending; merged is not accepted.",
  no_review: "No open review to accept.",
};

function stateLabel(state: Delivery["pr_state"]): string {
  return state === "unknown" ? "unknown (not read)" : state;
}

function shortSha(sha: string | null | undefined): string | null {
  return sha ? sha.slice(0, 7) : null;
}

/** The PR state read from the forge. `unknown` is its own badge, never styled as open. */
export function TaskDelivery({
  delivery,
  onChanged,
}: {
  delivery: Delivery;
  onChanged?: () => void;
}) {
  const [error, setError] = useState<string>();
  const action = useAction();
  const merged = delivery.pr_state === "merged";
  const head = shortSha(delivery.head?.sha);
  const combined = delivery.checks_combined;

  async function accept(decisionId: string) {
    await action.run(async () => {
      setError(undefined);
      try {
        await call(
          api.POST("/api/decisions/{decision_id}/answer", {
            params: { path: { decision_id: decisionId } },
            body: { choice: "accept", text: "", save_to_knowledge: null },
          }),
        );
        onChanged?.();
      } catch (err) {
        setError((err as Error).message);
      }
    });
  }

  return (
    <section className="delivery" aria-label="Delivery">
      <div>
        PR{" "}
        <a href={delivery.pr} target="_blank" rel="noreferrer">
          {delivery.pr.replace(/^https:\/\//, "")}
        </a>{" "}
        <span className={`badge pr-${delivery.pr_state}`}>{stateLabel(delivery.pr_state)}</span>
        {head && (
          <span className="muted small">
            {" "}
            head <code>{head}</code>
            {delivery.head?.source ? ` (${delivery.head.source})` : ""}
          </span>
        )}
        {combined && (
          <span className={`badge check-${combined}`} title="Exact-head checks combined">
            {" "}
            checks {combined}
          </span>
        )}
        {delivery.merged_at && (
          <span className="muted small">
            {" "}
            merged <DateChip at={delivery.merged_at} />
          </span>
        )}{" "}
        <span className="muted small">
          checked <DateChip at={delivery.checked_at} />
        </span>
      </div>
      {delivery.stale && (
        <p className="small warn">Stale head or off-head checks — re-read before acting.</p>
      )}
      {delivery.partial && delivery.partial.length > 0 && (
        <p className="muted small">Partial forge read: {delivery.partial.join(", ")}.</p>
      )}
      {delivery.flags.includes("delivery_merged_pending_review") && (
        <p className="small">
          <strong>Merged, pending review.</strong> A merge is not acceptance.
        </p>
      )}
      {delivery.authorization && <p className="muted small">{delivery.authorization.notes}</p>}
      {delivery.checklist && delivery.checklist.total > 0 && (
        <p className="muted small">
          Checklist {delivery.checklist.done}/{delivery.checklist.total} done
          {delivery.checklist.pending ? ` (${delivery.checklist.pending} pending)` : ""}.
        </p>
      )}
      {delivery.accept_decision_id && (
        <ActionButton
          type="button"
          pending={action.busy}
          action={() => accept(delivery.accept_decision_id as string)}
        >
          Accept
        </ActionButton>
      )}
      {merged && !delivery.accept_decision_id && delivery.accept_blocked && (
        <p className="muted small">{BLOCKED[delivery.accept_blocked]}</p>
      )}
      {error && <p className="error">{error}</p>}
    </section>
  );
}
