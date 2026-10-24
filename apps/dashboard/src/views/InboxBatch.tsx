import { useState } from "react";
import { api, call, type DecisionDetail } from "../api";

export const MAX_BATCH = 20; // the answers endpoint's limit

export type Draft = { choice: string | null; text: string };
export type Entry = { decision: DecisionDetail; draft: Draft };

type Outcome = {
  decision_id: string;
  ok: boolean;
  replayed?: boolean;
  error?: { message: string } | null;
};

/** What a decision still needs before it can be sent: an option when it has some, else text. */
export function isAnswerable({ decision, draft }: Entry): boolean {
  return decision.options ? draft.choice !== null : draft.text.trim() !== "";
}

export function describeAnswer({ draft }: Entry): string {
  return [draft.choice, draft.text.trim()].filter(Boolean).join(" — ");
}

/**
 * Confirmation and result of a batch. Nothing is sent until "Send"; each decision
 * carries its own answer. `requestIds` maps decision id to the request_id first
 * used for it, so sending a failed item again is a replay, never a second answer.
 */
export function BatchPanel(props: {
  entries: Entry[];
  requestIds: Map<string, string>;
  onResolved: (ids: string[]) => void;
  onClose: () => void;
}) {
  const [results, setResults] = useState<Record<string, Outcome>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const sent = Object.keys(results).length > 0;
  const failed = props.entries.filter((e) => results[e.decision.id]?.ok === false);

  function requestId(id: string): string {
    let existing = props.requestIds.get(id);
    if (!existing) {
      existing = crypto.randomUUID();
      props.requestIds.set(id, existing);
    }
    return existing;
  }

  async function send(entries: Entry[]) {
    setBusy(true);
    setError(undefined);
    try {
      const { results: outcomes } = await call(
        api.POST("/api/decisions/answers", {
          body: {
            answers: entries.map(({ decision, draft }) => ({
              decision_id: decision.id,
              choice: draft.choice,
              text: draft.text,
              request_id: requestId(decision.id),
            })),
          },
        }),
      );
      setResults((prev) => ({
        ...prev,
        ...Object.fromEntries(outcomes.map((o) => [o.decision_id, o])),
      }));
      props.onResolved(outcomes.filter((o) => o.ok).map((o) => o.decision_id));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card batch" aria-label="Confirm batch answers">
      <h3>
        {sent
          ? "Batch result"
          : `Confirm ${props.entries.length} answer${props.entries.length === 1 ? "" : "s"}`}
      </h3>
      {!sent && <p className="muted small">Nothing is sent until you confirm.</p>}
      <ol>
        {props.entries.map((e) => {
          const result = results[e.decision.id];
          return (
            <li key={e.decision.id}>
              <span className="badge">{e.decision.kind}</span> {e.decision.question}
              <div>
                Answer: <strong>{describeAnswer(e)}</strong>
              </div>
              {result?.ok && (
                <div className="muted small">ok{result.replayed ? " (already answered)" : ""}</div>
              )}
              {result && !result.ok && (
                <div className="error">Error: {result.error?.message ?? "failed"}</div>
              )}
            </li>
          );
        })}
      </ol>
      {error && <p className="error">{error}</p>}
      <div className="actions left">
        {!sent && (
          <button
            type="button"
            className="primary"
            disabled={busy}
            onClick={() => send(props.entries)}
          >
            Send {props.entries.length} answer{props.entries.length === 1 ? "" : "s"}
          </button>
        )}
        {sent && failed.length > 0 && (
          <button type="button" className="primary" disabled={busy} onClick={() => send(failed)}>
            Retry {failed.length} failed
          </button>
        )}
        {!sent && error && (
          <span className="muted small">A retry reuses the same request ids.</span>
        )}
        <button type="button" disabled={busy} onClick={props.onClose}>
          {sent ? "Close" : "Back"}
        </button>
      </div>
    </section>
  );
}
