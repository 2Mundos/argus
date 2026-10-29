import { useState } from "react";
import { api, call, download } from "../api";
import { ARCHIVABLE } from "../archive";
import { TaskDelivery } from "../delivery";
import { TaskFailover } from "../failover";
import { bytes, relative, shortId } from "../format";
import { useLiveData } from "../live";
import { Markdown } from "../Markdown";
import { TaskLabel } from "../TaskLabel";
import { pinText } from "../taskEdit";
import { Activity } from "./Activity";
import { AgentDetail } from "./AgentDetail";
import { EventList } from "./Events";
import { TaskChanges } from "./TaskChanges";
import { TaskEditForm } from "./TaskEditForm";
import { TaskLog } from "./TaskLog";

export function TaskDetail({
  taskId,
  onClose,
  variant = "panel",
}: {
  taskId: string;
  onClose: () => void;
  /** `panel` fills the main column (like a worker); `drawer` is the modal overlay. */
  variant?: "panel" | "drawer";
}) {
  const [error, setError] = useState<string>();
  const [confirmingRetry, setConfirmingRetry] = useState(false);
  const [agentId, setAgentId] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const { data, reload } = useLiveData(
    (signal) =>
      Promise.all([
        call(api.GET("/api/tasks/{task_id}", { params: { path: { task_id: taskId } }, signal })),
        call(
          api.GET("/api/tasks/{task_id}/artifacts", {
            params: { path: { task_id: taskId } },
            signal,
          }),
        ),
        call(
          api.GET("/api/events", {
            params: { query: { subject_id: taskId, limit: 50 } },
            signal,
          }),
        ),
        // A forge hiccup must not take the drawer down: no delivery section instead.
        call(
          api.GET("/api/tasks/{task_id}/delivery", {
            params: { path: { task_id: taskId } },
            signal,
          }),
        ).catch(() => null),
        call(
          api.GET("/api/tasks/{task_id}/models", {
            params: { path: { task_id: taskId } },
            signal,
          }),
        ),
      ]),
    [taskId],
  );
  const [task, artifacts, events, delivery, modelTrace] = data ?? [];
  const modelAttempts = [...new Set(modelTrace?.executions.map((run) => run.attempt) ?? [])].sort(
    (a, b) => b - a,
  );

  async function act(action: "cancel" | "unblock" | "retry" | "archive" | "unarchive") {
    setError(undefined);
    setConfirmingRetry(false);
    try {
      const path = { task_id: taskId };
      if (action === "cancel")
        await call(api.POST("/api/tasks/{task_id}/cancel", { params: { path } }));
      else if (action === "archive")
        await call(api.POST("/api/tasks/{task_id}/archive", { params: { path } }));
      else if (action === "unarchive")
        await call(api.POST("/api/tasks/{task_id}/unarchive", { params: { path } }));
      else if (action === "retry")
        await call(api.POST("/api/tasks/{task_id}/retry", { params: { path } }));
      else await call(api.POST("/api/tasks/{task_id}/unblock", { params: { path } }));
    } catch (err) {
      setError((err as Error).message);
    }
  }

  const titlebar = (
    <header className="panel-titlebar">
      <h2>{task ? <TaskLabel title={task.title} project={task.project} /> : "…"}</h2>
      <button type="button" className="icon-close" aria-label="Close" onClick={onClose}>
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path
            d="M3.2 3.2a.75.75 0 0 1 1.06 0L8 6.94l3.74-3.74a.75.75 0 1 1 1.06 1.06L9.06 8l3.74 3.74a.75.75 0 1 1-1.06 1.06L8 9.06l-3.74 3.74a.75.75 0 1 1-1.06-1.06L6.94 8 3.2 4.26a.75.75 0 0 1 0-1.06Z"
            fill="currentColor"
          />
        </svg>
      </button>
    </header>
  );

  const body = (
    <>
      {task && (
        <>
          <Activity task={task} events={events ?? []} />
          {delivery && <TaskDelivery delivery={delivery} onChanged={reload} />}
          <h3>Models</h3>
          {modelTrace?.executions.length ? (
            <div className="plain">
              {modelAttempts.map((attempt) => (
                <section key={attempt} aria-label={`Attempt ${attempt} models`}>
                  <h4>Attempt {attempt}</h4>
                  {modelTrace.executions
                    .filter((run) => run.attempt === attempt)
                    .map((run) => (
                      <p key={run.id}>
                        <strong>
                          {run.layer === "harness"
                            ? (run.engine ?? "harness")
                            : (run.executor ?? "executor")}
                        </strong>{" "}
                        {run.observed_model ?? run.configured_model ?? "not reported"}
                        {run.observed_model
                          ? ` (${run.evidence})`
                          : run.configured_model
                            ? " (configured)"
                            : ""}
                        {run.account_name
                          ? ` · account ${run.account_name}`
                          : run.account_id
                            ? ` · account ${run.account_id}`
                            : ""}{" "}
                        · {run.state}
                        {run.started_at ? ` · ${new Date(run.started_at).toISOString()}` : ""}
                        {run.ended_at ? ` → ${new Date(run.ended_at).toISOString()}` : ""}
                        {run.usage
                          ? ` · ${run.usage.input_tokens ?? 0}+${run.usage.output_tokens ?? 0} tokens · $${run.usage.cost_usd ?? "?"}`
                          : ""}
                      </p>
                    ))}
                  {modelTrace.timeline
                    .filter((selection) => selection.attempt === attempt)
                    .map((selection) => (
                      <p key={selection.id} className="muted small">
                        {selection.kind} · {selection.reason_code} ·{" "}
                        {selection.from_model ??
                          selection.from_account_name ??
                          selection.from_account_id ??
                          "start"}{" "}
                        →{" "}
                        {selection.to_model ??
                          selection.to_account_name ??
                          selection.to_account_id ??
                          "?"}
                        {selection.outcome ? ` · ${selection.outcome}` : ""}
                      </p>
                    ))}
                </section>
              ))}
            </div>
          ) : (
            <p className="muted">No model reports.</p>
          )}
          <dl className="facts">
            <dt>State</dt>
            <dd>
              <span className={`badge state-${task.state}`}>{task.state.replace("_", " ")}</span>
            </dd>
            <dt>Kind</dt>
            <dd>{task.kind}</dd>
            <dt>Difficulty</dt>
            <dd>
              <span className={`badge difficulty-${task.difficulty ?? "standard"}`}>
                {task.difficulty ?? "standard"}
              </span>{" "}
              <span className="muted small">({task.difficulty_source ?? "inferred"})</span>
            </dd>
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
            <TaskFailover task={task} />
            {task.assignee_agent_id && (
              <>
                <dt>Assignee</dt>
                <dd>
                  <button
                    type="button"
                    className="link"
                    onClick={() => setAgentId(task.assignee_agent_id)}
                  >
                    {shortId(task.assignee_agent_id)}
                  </button>{" "}
                  · lease {relative(task.lease_expires_at)}
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
            {task.account_providers && (
              <>
                <dt>Runs on</dt>
                <dd>{task.account_providers.join(", ")}</dd>
              </>
            )}
            {task.harness_account && (
              <>
                <dt>Harness account</dt>
                <dd>{task.harness_account}</dd>
              </>
            )}
            {task.harness_model && (
              <>
                <dt>Harness</dt>
                <dd>{pinText(task.harness_model)}</dd>
              </>
            )}
            {task.executor_model && (
              <>
                <dt>Executor</dt>
                <dd>{pinText(task.executor_model)}</dd>
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
            {(task.editable_fields ?? []).length > 0 && !editing && (
              <button type="button" onClick={() => setEditing(true)}>
                Edit
              </button>
            )}
            {task.state === "blocked" && (
              <button type="button" onClick={() => act("unblock")}>
                Unblock
              </button>
            )}
            {task.state === "failed" &&
              (confirmingRetry ? (
                <>
                  <span className="muted small">
                    Reset attempts ({task.attempt}/{task.max_attempts} used) and run it again?
                  </span>
                  <button type="button" className="primary" onClick={() => act("retry")}>
                    Confirm retry
                  </button>
                  <button type="button" onClick={() => setConfirmingRetry(false)}>
                    Keep failed
                  </button>
                </>
              ) : (
                <button type="button" onClick={() => setConfirmingRetry(true)}>
                  Retry
                </button>
              ))}
            {ARCHIVABLE.includes(task.state) &&
              (task.archived_at ? (
                <button type="button" onClick={() => act("unarchive")}>
                  Unarchive
                </button>
              ) : (
                <button type="button" onClick={() => act("archive")}>
                  Archive
                </button>
              ))}
            {!ARCHIVABLE.includes(task.state) && (
              <button type="button" className="danger" onClick={() => act("cancel")}>
                Cancel task
              </button>
            )}
          </div>
          {error && <p className="error">{error}</p>}
          {task.error && <p className="error">Last error: {task.error}</p>}
          {editing ? (
            <TaskEditForm
              task={task}
              onDone={(saved) => {
                setEditing(false);
                if (saved) reload();
              }}
            />
          ) : (
            <>
              <h3>Spec</h3>
              <div className="spec">
                <Markdown>{task.spec || "_No spec._"}</Markdown>
              </div>
            </>
          )}
          {task.attempt > 0 && (
            <TaskLog
              taskId={taskId}
              attempt={task.attempt}
              steer={{
                claimed: task.state === "claimed",
                assigneeId: task.assignee_agent_id,
              }}
            />
          )}
          {task.attempt > 0 && task.required_capabilities.includes("repository:write") && (
            <TaskChanges taskId={taskId} attempt={task.attempt} />
          )}
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
                      {a.source === "attachment" ? "attachment" : `attempt ${a.attempt}`} ·{" "}
                      {bytes(a.size)} · {a.media_type} · {a.source}
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
    </>
  );

  const nested = agentId ? (
    <AgentDetail agentId={agentId} onClose={() => setAgentId(null)} />
  ) : null;

  if (variant === "panel") {
    return (
      <>
        <section className="task-panel" aria-label="Task detail">
          {titlebar}
          <div className="panel-body">{body}</div>
        </section>
        {nested}
      </>
    );
  }

  return (
    <>
      <div className="overlay" onClick={onClose} onKeyDown={(e) => e.key === "Escape" && onClose()}>
        <aside
          className="drawer task-drawer"
          onClick={(e) => e.stopPropagation()}
          onKeyDown={() => {}}
        >
          {titlebar}
          <div className="panel-body">{body}</div>
        </aside>
      </div>
      {nested}
    </>
  );
}
