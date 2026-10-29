import { useRef, useState } from "react";
import { api, call, type DecisionDetail, type InboxGroup, type InboxItem } from "../api";
import { blockReasonText } from "../blockCopy";
import { actor, relative, shortId } from "../format";
import { useLiveData } from "../live";
import { Markdown } from "../Markdown";
import { dur, quietFacts } from "../stall";
import { TaskLabel } from "../TaskLabel";
import { BatchPanel, type Draft, type Entry, isAnswerable, MAX_BATCH } from "./InboxBatch";
import { TaskDetail } from "./TaskDetail";
import { TaskSteers } from "./TaskSteers";

const KIND_ORDER = ["question", "approval", "review", "knowledge", "policy", "none"] as const;

function itemKind(item: InboxItem): string {
  return item.decision_kind ?? "none";
}

function kindLabel(kind: string): string {
  if (kind === "none") return "No decision";
  return kind.charAt(0).toUpperCase() + kind.slice(1);
}

function kindSections(
  groups: InboxGroup[],
  byGroup: Map<string, InboxItem[]>,
  totals: Record<string, number> | undefined,
) {
  const byKind = new Map<string, Map<string, InboxGroup[]>>();
  for (const group of groups) {
    const present = new Set((byGroup.get(group.key) ?? []).map(itemKind));
    for (const kind of present) {
      const projects = byKind.get(kind) ?? new Map<string, InboxGroup[]>();
      const name = group.project ?? "Pools";
      const list = projects.get(name) ?? [];
      list.push(group);
      projects.set(name, list);
      byKind.set(kind, projects);
    }
  }
  const rank = new Map<string, number>(KIND_ORDER.map((kind, index) => [kind, index]));
  return [...byKind.keys()]
    .sort((a, b) => (rank.get(a) ?? 99) - (rank.get(b) ?? 99) || a.localeCompare(b))
    .map((kind) => ({
      kind,
      count:
        totals?.[kind] ??
        [...byGroup.values()].flat().filter((item) => itemKind(item) === kind).length,
      projects: [...(byKind.get(kind) ?? [])],
    }));
}

export function Inbox() {
  const [order, setOrder] = useState<"age" | "impact">("age");
  const [blockingOnly, setBlockingOnly] = useState(false);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [selected, setSelected] = useState<string[]>([]);
  // The entries under confirmation, frozen when the step opens so results stay listed.
  const [batch, setBatch] = useState<Entry[] | null>(null);
  const confirming = batch !== null;
  const [taskDrawer, setTaskDrawer] = useState<string | null>(null);
  const requestIds = useRef(new Map<string, string>());
  const { data: page, error } = useLiveData(
    (signal) =>
      call(
        api.GET("/api/inbox", {
          params: { query: { view: "detail", limit: 100, order, blocking_only: blockingOnly } },
          signal,
        }),
      ),
    [order, blockingOnly],
  );
  const draftOf = (id: string): Draft => drafts[id] ?? { choice: null, text: "" };
  const byGroup = new Map<string, InboxItem[]>();
  for (const item of page?.items ?? [])
    byGroup.set(item.group, [...(byGroup.get(item.group) ?? []), item]);
  const entries: Entry[] = selected.flatMap((id) => {
    const decision = page?.items.find((i) => i.decision?.id === id)?.decision;
    return decision ? [{ decision, draft: draftOf(id) }] : [];
  });
  const ready = entries.length > 0 && entries.every(isAnswerable);
  const kinds = kindSections(page?.groups ?? [], byGroup, page?.totals_by_decision_kind);
  if (taskDrawer) {
    return (
      <section className="task-panel-page">
        <TaskDetail taskId={taskDrawer} variant="panel" onClose={() => setTaskDrawer(null)} />
      </section>
    );
  }
  return (
    <section>
      <div className="toolbar">
        <h2>Inbox</h2>
        <label>
          Sort
          <select value={order} onChange={(e) => setOrder(e.target.value as "age" | "impact")}>
            <option value="age">Oldest first</option>
            <option value="impact">Most blocked tasks first</option>
          </select>
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={blockingOnly}
            onChange={(e) => setBlockingOnly(e.target.checked)}
          />
          Blocking only
        </label>
      </div>
      <p className="page-lead">Decisions waiting for an answer.</p>
      {error && <p className="error">{error.message}</p>}
      {page?.items.length === 0 && (
        <p className="muted empty-state">Nothing waiting for a decision.</p>
      )}
      {page && page.total > page.items.length && (
        <p className="muted small">
          Showing the first {page.items.length} of {page.total} rows.
        </p>
      )}
      {confirming ? (
        <BatchPanel
          entries={batch}
          requestIds={requestIds.current}
          onResolved={(ids) => {
            for (const id of ids) requestIds.current.delete(id);
            setSelected((prev) => prev.filter((id) => !ids.includes(id)));
          }}
          onClose={() => setBatch(null)}
        />
      ) : (
        selected.length > 0 && (
          <div className="card batch-bar">
            {selected.length} selected (max {MAX_BATCH}){" "}
            <button
              type="button"
              className="primary"
              disabled={!ready}
              onClick={() => setBatch(entries)}
            >
              Review {selected.length} answer{selected.length === 1 ? "" : "s"}
            </button>
            {!ready && (
              <span className="muted small"> Every selected decision needs its own answer.</span>
            )}
          </div>
        )
      )}
      {kinds.map(({ kind, count, projects: kindProjects }) => (
        <div key={kind} className="inbox-kind">
          <h3>
            {kindLabel(kind)} <span className="nav-count">{count}</span>
          </h3>
          {kindProjects.map(([project, groups]) => (
            <div key={project} className="inbox-project">
              <h3>{project}</h3>
              {groups.map((g) => (
                <div key={g.key} className="inbox-group">
                  <h4>
                    {g.title ?? g.subject_type.replace("knowledge_", "")}{" "}
                    <span className="muted small">
                      {g.open_decisions} open · oldest {dur(g.oldest_age_s)} · {g.stalled_tasks}{" "}
                      stalled task{g.stalled_tasks === 1 ? "" : "s"}
                    </span>
                  </h4>
                  {(byGroup.get(g.key) ?? [])
                    .filter((item) => itemKind(item) === kind)
                    .map((item) =>
                      item.decision ? (
                        <DecisionCard
                          key={item.decision.id}
                          decision={item.decision}
                          item={item}
                          draft={draftOf(item.decision.id)}
                          onDraft={(d) =>
                            setDrafts((prev) => ({ ...prev, [item.decision!.id]: d }))
                          }
                          selected={selected.includes(item.decision.id)}
                          canSelect={selected.length < MAX_BATCH && !confirming}
                          onSelect={(on) => {
                            const id = item.decision!.id;
                            setSelected((prev) =>
                              on ? [...prev, id] : prev.filter((x) => x !== id),
                            );
                          }}
                        />
                      ) : item.state === "claimed" ? (
                        <QuietClaimCard
                          key={item.subject_id}
                          item={item}
                          onOpenTask={() => setTaskDrawer(item.subject_id)}
                        />
                      ) : item.failed_dependencies.length > 0 ? (
                        <p key={item.subject_id} className="muted small">
                          {item.title ?? shortId(item.subject_id)} is stalled behind{" "}
                          {item.failed_dependencies.map(shortId).join(", ")}.
                        </p>
                      ) : (
                        <BlockedCard
                          key={item.subject_id}
                          item={item}
                          onOpenTask={() => setTaskDrawer(item.subject_id)}
                        />
                      ),
                    )}
                </div>
              ))}
            </div>
          ))}
        </div>
      ))}
    </section>
  );
}

function BlockedCard({ item, onOpenTask }: { item: InboxItem; onOpenTask: () => void }) {
  const reason = blockReasonText(item.task?.block_reason);
  return (
    <article className="card">
      <p>
        <TaskLabel title={item.title ?? shortId(item.subject_id)} project={item.project} /> is
        blocked{reason ? `: ${reason}` : ""}.
      </p>
      {item.task?.error && <p className="small">{item.task.error}</p>}
      <div className="actions left">
        <button type="button" className="link" onClick={onOpenTask}>
          Open task
        </button>
      </div>
    </article>
  );
}

/** What the worker host last saw for a quiet claim (stall visibility 3.7). */
function QuietClaimCard({ item, onOpenTask }: { item: InboxItem; onOpenTask: () => void }) {
  const [steerOpen, setSteerOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const task = item.task;
  const attempt = item.attempt ?? task?.attempt ?? 1;
  const assigneeId = task?.assignee_agent_id ?? null;

  const quietLine = quietFacts({
    age_s: item.age_s,
    last_signal_at: item.last_signal_at,
    last_signal_source: item.last_signal_source,
    stall_cause: item.stall_cause,
  });

  async function release() {
    setBusy(true);
    setError(undefined);
    try {
      await call(
        api.POST("/api/tasks/{task_id}/release", {
          params: { path: { task_id: item.subject_id } },
          body: { reason: "quiet in inbox", attempt: item.attempt ?? undefined },
        }),
      );
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <article className="card quiet-claim">
      <p>
        <TaskLabel title={item.title ?? shortId(item.subject_id)} project={item.project} /> is{" "}
        {quietLine}
      </p>
      {error && <p className="error">{error}</p>}
      <div className="actions left">
        <button type="button" className="link" onClick={onOpenTask}>
          Open task
        </button>
        <button type="button" disabled={busy} onClick={() => void release()}>
          Release
        </button>
        <button type="button" disabled={busy} onClick={() => setSteerOpen((o) => !o)}>
          {steerOpen ? "Close steer" : "Steer"}
        </button>
      </div>
      <TaskSteers
        taskId={item.subject_id}
        attempt={attempt}
        claimed={true}
        assigneeId={assigneeId}
        composerOpen={steerOpen}
        onComposerOpenChange={setSteerOpen}
      />
    </article>
  );
}

/** Task subjects get "<Project> - <title>"; the project is not on the decision, so it is looked up. */
function DecisionSubject({ decision }: { decision: DecisionDetail }) {
  const isTask = decision.subject_type === "task" && decision.subject_title != null;
  const { data: task } = useLiveData(
    (signal) =>
      isTask
        ? call(
            api.GET("/api/tasks/{task_id}", {
              params: { path: { task_id: decision.subject_id } },
              signal,
            }),
          )
        : Promise.resolve(null),
    [decision.subject_id],
  );
  if (isTask && task) return <TaskLabel title={task.title} project={task.project} />;
  return <>{decision.subject_title ?? shortId(decision.subject_id)}</>;
}

function DecisionCard(props: {
  decision: DecisionDetail;
  item: InboxItem;
  draft: Draft;
  onDraft: (draft: Draft) => void;
  selected: boolean;
  canSelect: boolean;
  onSelect: (on: boolean) => void;
}) {
  const { decision, item, draft, selected } = props;
  const text = draft.text;
  const setText = (value: string) => props.onDraft({ ...draft, text: value });
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
          <strong>
            <DecisionSubject decision={decision} />
          </strong>{" "}
          · {relative(decision.created_at)} by {actor(decision.requested_by)}
        </span>
      </header>
      <p className="muted small">
        {item.impact > 0
          ? `Blocks ${item.impact} task${item.impact === 1 ? "" : "s"}: ${item.blocks.map((b) => b.title).join(", ")}`
          : "Blocks no tasks"}
      </p>
      <p className="question">{decision.question}</p>
      {decision.kind === "knowledge" && <ProposalPreview proposalId={decision.subject_id} />}
      <label>
        {textLabel} {decision.options ? "(optional)" : ""}
        <textarea rows={2} value={text} onChange={(e) => setText(e.target.value)} />
      </label>
      {decision.kind === "question" && !selected && (
        <SaveToKnowledge value={saveTo} onChange={setSaveTo} />
      )}
      <div className="row">
        <label className="check">
          <input
            type="checkbox"
            checked={selected}
            disabled={!selected && !props.canSelect}
            onChange={(e) => props.onSelect(e.target.checked)}
          />
          Add to batch
        </label>
        {selected && decision.options && (
          <select
            aria-label={`Batch answer for ${decision.question}`}
            value={draft.choice ?? ""}
            onChange={(e) => props.onDraft({ ...draft, choice: e.target.value || null })}
          >
            <option value="">Choose an answer…</option>
            {decision.options.map((o) => (
              <option key={o} value={o}>
                {o}
              </option>
            ))}
          </select>
        )}
      </div>
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
    (signal) =>
      call(
        api.GET("/api/knowledge/proposals/{proposal_id}", {
          params: { path: { proposal_id: proposalId } },
          signal,
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
  const { data: pages } = useLiveData(
    (signal) => call(api.GET("/api/knowledge/pages", { signal })),
    [],
  );
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
