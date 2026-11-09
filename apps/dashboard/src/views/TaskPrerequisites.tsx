import type { components } from "@argus/api-client";
import { useEffect, useRef, useState } from "react";
import { api, call } from "../api";
import { DateChip } from "../DateChip";

type View = components["schemas"]["PrerequisitesView"];
type Item = components["schemas"]["PrerequisiteItem"];

function ItemRow({ item }: { item: Item }) {
  const { evidence } = item;
  const artifacts = evidence.artifacts ?? [];
  return (
    <li className="task-prereq-item">
      <a href={`#tasks/${item.task_id}`}>{item.title}</a>
      <dl className="task-prereq-facts small">
        <dt>Task state</dt>
        <dd>
          {item.state} ({item.task_succeeded ? "succeeded" : "not succeeded"})
        </dd>
        <dt>Delivery verdict</dt>
        <dd>
          <span className={`badge prereq-${item.verdict}`}>{item.verdict}</span> · why: {item.why}
        </dd>
        <dt>Checked</dt>
        <dd>
          {evidence.checked_at ? (
            <DateChip at={evidence.checked_at} />
          ) : artifacts.some((a) => a.checked_at) ? (
            "PR not checked"
          ) : (
            "not checked"
          )}
          {evidence.source ? ` · source: ${evidence.source}` : ""}
        </dd>
        {evidence.pr && (
          <>
            <dt>Pull request</dt>
            <dd>
              {evidence.pr} ({evidence.pr_state ?? "unknown"})
              {evidence.base_ref ? ` · base ${evidence.base_ref}` : ""}
              {evidence.head_sha ? ` · head ${evidence.head_sha.slice(0, 8)}` : ""}
              {evidence.merged_at ? " · merged " : ""}
              {evidence.merged_at && <DateChip at={evidence.merged_at} />}
            </dd>
          </>
        )}
        {artifacts.length > 0 && (
          <>
            <dt>Artifacts</dt>
            <dd>
              <ul>
                {artifacts.map((a) => (
                  <li key={a.id}>
                    {a.name} · verified: {String(a.verified)} · {a.size} bytes ·{" "}
                    {a.checked_at ? (
                      <>
                        checked <DateChip at={a.checked_at} />
                      </>
                    ) : (
                      "checked: unknown"
                    )}
                  </li>
                ))}
              </ul>
            </dd>
          </>
        )}
        {(evidence.notes ?? []).length > 0 && (
          <>
            <dt>Notes</dt>
            <dd>{(evidence.notes ?? []).join("; ")}</dd>
          </>
        )}
      </dl>
    </li>
  );
}

function Body({ view }: { view: View }) {
  const partial = view.partial ?? [];
  const omitted = view.omitted ?? [];
  return (
    <>
      <p className="muted small">
        Evidence only (enforced: {String(view.enforced ?? false)}). It grants no claim, merge or
        acceptance permission. A succeeded dependency is not the same as a delivered one.
      </p>
      {(partial.length > 0 || omitted.length > 0) && (
        <p className="error small">
          Partial result.
          {partial.length > 0 && ` Incomplete reads: ${partial.join(", ")}.`}
          {omitted.length > 0 && ` Omitted (truncated): ${omitted.join(", ")}.`}
        </p>
      )}
      {view.items.length === 0 ? (
        <p className="muted small">No direct dependencies.</p>
      ) : (
        <ul className="task-prereq-list">
          {view.items.map((item) => (
            <ItemRow key={item.task_id} item={item} />
          ))}
        </ul>
      )}
    </>
  );
}

function Loader({ taskId }: { taskId: string }) {
  const [view, setView] = useState<View>();
  const [error, setError] = useState<Error>();
  const [loading, setLoading] = useState(true);
  const controller = useRef<AbortController | undefined>(undefined);

  const load = () => {
    controller.current?.abort();
    const ac = new AbortController();
    controller.current = ac;
    setLoading(true);
    setError(undefined);
    call(
      api.GET("/api/tasks/{task_id}/prerequisites", {
        params: { path: { task_id: taskId } },
        signal: ac.signal,
      }),
    )
      .then((v) => {
        if (ac.signal.aborted) return;
        setView(v);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (ac.signal.aborted) return;
        setError(e instanceof Error ? e : new Error(String(e)));
        setLoading(false);
      });
  };

  // One request when the panel opens; the cleanup drops it if the panel unmounts.
  useEffect(() => {
    load();
    return () => controller.current?.abort();
  }, [taskId]);

  return (
    <>
      {error && (
        <p className="error small">
          Could not load prerequisites: {error.message}{" "}
          <button type="button" onClick={load}>
            Retry
          </button>
        </p>
      )}
      {loading && !error && <p className="muted small">Loading…</p>}
      {view && !error && (
        <>
          <Body view={view} />
          <button type="button" disabled={loading} onClick={load}>
            Refresh
          </button>
        </>
      )}
    </>
  );
}

/**
 * Read-only delivery evidence for a task's direct dependencies (task discovery spec 3.2).
 * Fetched once on first open and again only on an explicit Retry or Refresh: the
 * view reads the forge, so it never rides the live event refresh.
 */
export function TaskPrerequisites({ taskId }: { taskId: string }) {
  const [opened, setOpened] = useState(false);
  return (
    <details
      className="task-prereq"
      aria-label="Dependency delivery"
      onToggle={(e) => e.currentTarget.open && setOpened(true)}
    >
      <summary className="task-section-label">Dependency delivery</summary>
      {opened && <Loader taskId={taskId} />}
    </details>
  );
}
