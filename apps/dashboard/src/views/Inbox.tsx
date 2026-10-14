import { useState } from "react";
import { api, call, type Decision } from "../api";
import { actor, relative, shortId } from "../format";
import { useLiveData } from "../live";

export function Inbox() {
  const { data: decisions, error } = useLiveData(() => call(api.GET("/api/decisions")), []);
  return (
    <section>
      <div className="toolbar">
        <h2>Inbox</h2>
      </div>
      {error && <p className="error">{error.message}</p>}
      {decisions?.length === 0 && <p className="muted">Nothing waiting for a decision.</p>}
      {(decisions ?? []).map((d) => (
        <DecisionCard key={d.id} decision={d} />
      ))}
    </section>
  );
}

function DecisionCard({ decision }: { decision: Decision }) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  async function answer(choice?: string) {
    setBusy(true);
    setError(undefined);
    try {
      await call(
        api.POST("/api/decisions/{decision_id}/answer", {
          params: { path: { decision_id: decision.id } },
          body: { choice: choice ?? null, text },
        }),
      );
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  const textLabel =
    decision.kind === "question" ? "Answer" : decision.kind === "review" ? "Notes" : "Reason";
  return (
    <article className="card decision">
      <header>
        <span className="badge">{decision.kind}</span>
        <span className="muted small">
          for {decision.audience} · {decision.subject_type}{" "}
          <strong>{decision.subject_title ?? shortId(decision.subject_id)}</strong> ·{" "}
          {relative(decision.created_at)} by {actor(decision.requested_by)}
        </span>
      </header>
      <p className="question">{decision.question}</p>
      <label>
        {textLabel} {decision.options ? "(optional)" : ""}
        <textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      {error && <p className="error">{error}</p>}
      <div className="actions left">
        {decision.options ? (
          decision.options.map((option) => (
            <button
              type="button"
              key={option}
              disabled={busy}
              className={option === "reject" ? "danger" : "primary"}
              onClick={() => answer(option)}
            >
              {option}
            </button>
          ))
        ) : (
          <button
            type="button"
            className="primary"
            disabled={busy || !text.trim()}
            onClick={() => answer()}
          >
            Send answer
          </button>
        )}
      </div>
    </article>
  );
}
