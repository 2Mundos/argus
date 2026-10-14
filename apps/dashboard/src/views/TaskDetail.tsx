import { useState } from "react";
import { api, call, download } from "../api";
import { bytes, relative, shortId } from "../format";
import { useLiveData } from "../live";
import { Markdown } from "../Markdown";
import { EventList } from "./Events";

export function TaskDetail({ taskId, onClose }: { taskId: string; onClose: () => void }) {
  const [error, setError] = useState<string>();
  const { data } = useLiveData(
    () =>
      Promise.all([
        call(api.GET("/api/tasks/{task_id}", { params: { path: { task_id: taskId } } })),
        call(api.GET("/api/tasks/{task_id}/artifacts", { params: { path: { task_id: taskId } } })),
        call(api.GET("/api/events", { params: { query: { subject_id: taskId, limit: 50 } } })),
      ]),
    [taskId],
  );
  const [task, artifacts, events] = data ?? [];

  async function act(action: "cancel" | "unblock") {
    setError(undefined);
    try {
      const path = { task_id: taskId };
      if (action === "cancel")
        await call(api.POST("/api/tasks/{task_id}/cancel", { params: { path } }));
      else await call(api.POST("/api/tasks/{task_id}/unblock", { params: { path } }));
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div className="overlay" onClick={onClose} onKeyDown={(e) => e.key === "Escape" && onClose()}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()} onKeyDown={() => {}}>
        <div className="toolbar">
          <h2>{task?.title ?? "…"}</h2>
          <button type="button" className="link" onClick={onClose}>
            Close
          </button>
        </div>
        {task && (
          <>
            <dl className="facts">
              <dt>State</dt>
              <dd>
                <span className={`badge state-${task.state}`}>{task.state.replace("_", " ")}</span>
              </dd>
              <dt>Kind</dt>
              <dd>{task.kind}</dd>
              {task.project && (
                <>
                  <dt>Project</dt>
                  <dd>{task.project}</dd>
                </>
              )}
              {task.knowledge.length > 0 && (
                <>
                  <dt>Pinned pages</dt>
                  <dd>{task.knowledge.map(shortId).join(", ")}</dd>
                </>
              )}
              <dt>Attempt</dt>
              <dd>
                {task.attempt} (failures {task.failures}/{task.max_attempts})
              </dd>
              {task.assignee_agent_id && (
                <>
                  <dt>Assignee</dt>
                  <dd>
                    {shortId(task.assignee_agent_id)} · lease {relative(task.lease_expires_at)}
                  </dd>
                </>
              )}
              {task.timeout_at && (
                <>
                  <dt>Times out</dt>
                  <dd>{relative(task.timeout_at)}</dd>
                </>
              )}
              {task.required_capabilities.length > 0 && (
                <>
                  <dt>Needs</dt>
                  <dd>{task.required_capabilities.join(", ")}</dd>
                </>
              )}
              {task.depends_on.length > 0 && (
                <>
                  <dt>Depends on</dt>
                  <dd>{task.depends_on.map(shortId).join(", ")}</dd>
                </>
              )}
              <dt>Created</dt>
              <dd>
                {relative(task.created_at)} by {task.created_by}
              </dd>
            </dl>
            <div className="actions left">
              {task.state === "blocked" && (
                <button type="button" onClick={() => act("unblock")}>
                  Unblock
                </button>
              )}
              {!["succeeded", "failed", "cancelled"].includes(task.state) && (
                <button type="button" className="danger" onClick={() => act("cancel")}>
                  Cancel task
                </button>
              )}
            </div>
            {error && <p className="error">{error}</p>}
            {task.error && <p className="error">Last error: {task.error}</p>}
            <h3>Spec</h3>
            <div className="spec">
              <Markdown>{task.spec || "_No spec._"}</Markdown>
            </div>
            {task.result && (
              <>
                <h3>Result</h3>
                <Markdown>{task.result.summary}</Markdown>
                {task.result.data && <pre>{JSON.stringify(task.result.data, null, 2)}</pre>}
              </>
            )}
            {artifacts && artifacts.length > 0 && (
              <>
                <h3>Artifacts</h3>
                <ul className="plain">
                  {artifacts.map((a) => (
                    <li key={a.id}>
                      <button
                        type="button"
                        className="link"
                        onClick={() => download(`/api/artifacts/${a.id}/content`, a.name)}
                      >
                        {a.name}
                      </button>{" "}
                      <span className="muted small">
                        attempt {a.attempt} · {bytes(a.size)}
                      </span>
                    </li>
                  ))}
                </ul>
              </>
            )}
            <h3>History</h3>
            <EventList events={events ?? []} />
          </>
        )}
      </aside>
    </div>
  );
}
