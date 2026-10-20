import { useState } from "react";
import { api, call, type DecisionDetail } from "../api";
import { actor, relative, shortId } from "../format";
import { useLiveData } from "../live";
import { Markdown } from "../Markdown";

export function Inbox() {
  const { data: page, error } = useLiveData(
    () => call(api.GET("/api/decisions", { params: { query: { view: "detail", limit: 100 } } })),
    [],
  );
  // view=detail returns only details; the guard narrows the generated union.
  const decisions = page?.items.filter((d): d is DecisionDetail => "question" in d);
  return (
    <section>
      <div className="toolbar">
        <h2>Inbox</h2>
      </div>
      {error && <p className="error">{error.message}</p>}
      {decisions?.length === 0 && <p className="muted">Nothing waiting for a decision.</p>}
      {page && page.total > page.items.length && (
        <p className="muted small">
          Showing the oldest {page.items.length} of {page.total} open decisions.
        </p>
      )}
      {(decisions ?? []).map((d) => (
        <DecisionCard key={d.id} decision={d} />
      ))}
    </section>
  );
}

function DecisionCard({ decision }: { decision: DecisionDetail }) {
  const [text, setText] = useState("");
  const [saveTo, setSaveTo] = useState<string | null>(null); // null: don't save; "": new page
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  async function answer(choice?: string) {
    setBusy(true);
    setError(undefined);
    try {
      await call(
        api.POST("/api/decisions/{decision_id}/answer", {
          params: { path: { decision_id: decision.id } },
          body: {
            choice: choice ?? null,
            text,
            save_to_knowledge: saveTo === null ? null : saveTo === "" ? {} : { page_id: saveTo },
          },
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
          for {decision.audience} · {decision.subject_type.replace("knowledge_", "")}{" "}
          <strong>{decision.subject_title ?? shortId(decision.subject_id)}</strong> ·{" "}
          {relative(decision.created_at)} by {actor(decision.requested_by)}
        </span>
      </header>
      <p className="question">{decision.question}</p>
      {decision.kind === "knowledge" && <ProposalPreview proposalId={decision.subject_id} />}
      <label>
        {textLabel} {decision.options ? "(optional)" : ""}
        <textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      {decision.kind === "question" && <SaveToKnowledge value={saveTo} onChange={setSaveTo} />}
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

function ProposalPreview({ proposalId }: { proposalId: string }) {
  const { data: proposal } = useLiveData(
    () =>
      call(
        api.GET("/api/knowledge/proposals/{proposal_id}", {
          params: { path: { proposal_id: proposalId } },
        }),
      ),
    [proposalId],
  );
  if (!proposal) return null;
  return (
    <details className="proposal">
      <summary>
        Proposed {proposal.page_id ? "revision" : "page"}: <strong>{proposal.title}</strong>{" "}
        <span className="muted small">in {proposal.project}</span>
      </summary>
      {proposal.summary && <p className="summary">{proposal.summary}</p>}
      <Markdown>{proposal.body || "_Empty._"}</Markdown>
    </details>
  );
}

function SaveToKnowledge(props: { value: string | null; onChange: (v: string | null) => void }) {
  const { data: pages } = useLiveData(() => call(api.GET("/api/knowledge/pages")), []);
  return (
    <div className="row">
      <label className="check">
        <input
          type="checkbox"
          checked={props.value !== null}
          onChange={(e) => props.onChange(e.target.checked ? "" : null)}
        />
        Also save the answer to knowledge
      </label>
      {props.value !== null && (
        <select value={props.value} onChange={(e) => props.onChange(e.target.value)}>
          <option value="">as a new page (in the task's project)</option>
          {(pages ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              append to: {p.title} ({p.project})
            </option>
          ))}
        </select>
      )}
    </div>
  );
}
