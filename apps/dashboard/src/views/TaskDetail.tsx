import { useEffect, useRef, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { api, call, download } from "../api";
import { ARCHIVABLE } from "../archive";
import { blockReasonText } from "../blockCopy";
import { DateChip } from "../DateChip";
import { deliverableLabel } from "../deliverables";
import { TaskDelivery } from "../delivery";
import { TaskFailover } from "../failover";
import { bytes, shortId } from "../format";
import { useLiveData } from "../live";
import { Markdown } from "../Markdown";
import { TaskLabel } from "../TaskLabel";
import { pinText } from "../taskEdit";
import { WorkerChip } from "../WorkerChip";
import { Activity } from "./Activity";
import { AttemptStrip } from "./AttemptStrip";
import { EventList } from "./EventList";
import { TaskTerminal } from "./TaskTerminal";
import { TaskSteers } from "./TaskSteers";

/** Size of the History page; a full page may hide older events. */
const HISTORY_LIMIT = 50;
const ERROR_EXCERPT = 160;

function LastError({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const needsMore = text.length > ERROR_EXCERPT;
  const shown = open || !needsMore ? text : `${text.slice(0, ERROR_EXCERPT).trimEnd()}…`;
  return (
    <p className="error last-error">
      Last error: {shown}
      {needsMore && (
        <>
          {" "}
          <button type="button" className="link" onClick={() => setOpen((v) => !v)}>
            {open ? "Show less" : "Read more"}
          </button>
        </>
      )}
    </p>
  );
}

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
  const [editing, setEditing] = useState(false);
  const [picked, setPicked] = useState<number | null>(null);
  const [steerOpen, setSteerOpen] = useState(false);
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
            params: { query: { subject_id: taskId, limit: HISTORY_LIMIT } },
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
        // The strip is an aid: without it the current attempt stays selected.
        call(
          api.GET("/api/tasks/{task_id}/attempts", {
            params: { path: { task_id: taskId } },
            signal,
          }),
        ).catch(() => []),
      ]),
    [taskId],
  );
  const [task, artifacts, events, delivery, modelTrace, attemptList] = data ?? [];
  const current = task?.attempt ?? 0;
  const prevTaskId = useRef(taskId);
  const prevCurrent = useRef<number | null>(null);
  // A new current attempt (or another task) re-selects the current one.
  useEffect(() => {
    setPicked(null);
    const taskChanged = prevTaskId.current !== taskId;
    if (taskChanged) {
      setSteerOpen(false);
      prevCurrent.current = null;
    } else if (prevCurrent.current !== null && prevCurrent.current !== current) {
      setSteerOpen(false);
    }
    prevTaskId.current = taskId;
    if (current > 0) prevCurrent.current = current;
  }, [taskId, current]);
  const attemptNumbers = Array.isArray(attemptList) ? attemptList.map((a) => a.attempt) : [];
  const steerable = task?.state === "claimed" && current > 0;
  const assigneeId = task?.assignee_agent_id ?? null;
  const { data: assignee, error: assigneeError } = useLiveData(
    async (signal) =>
      steerable && assigneeId
        ? call(
            api.GET("/api/agents/{agent_id}", {
              params: { path: { agent_id: assigneeId } },
              signal,
            }),
          )
        : null,
    [steerable, assigneeId],
  );
  // Unknown until the assignee loads; a failed lookup counts as unsupported.
  const supportsSteer = !steerable
    ? undefined
    : assignee
      ? (assignee.runtime?.features?.includes("steer") ?? false)
      : assigneeError
        ? false
        : undefined;
  const selected = picked !== null && attemptNumbers.includes(picked) ? picked : current;
  // Numbers restart after a retry: the newest row with the number is the one shown.
  const unknownSteers = Array.isArray(attemptList)
    ? (attemptList.filter((a) => a.attempt === selected).at(-1)?.steer_unknown ?? 0)
    : 0;
  const modelAttempts = [...new Set(modelTrace?.executions.map((run) => run.attempt) ?? [])].sort(
    (a, b) => b - a,
  );
  const action = useAction();

  async function act(kind: "cancel" | "unblock" | "retry" | "archive" | "unarchive") {
    setError(undefined);
    setConfirmingRetry(false);
    await action.run(async () => {
      try {
        const path = { task_id: taskId };
        if (kind === "cancel")
          await call(api.POST("/api/tasks/{task_id}/cancel", { params: { path } }));
        else if (kind === "archive")
          await call(api.POST("/api/tasks/{task_id}/archive", { params: { path } }));
        else if (kind === "unarchive")
          await call(api.POST("/api/tasks/{task_id}/unarchive", { params: { path } }));
        else if (kind === "retry")
          await call(api.POST("/api/tasks/{task_id}/retry", { params: { path } }));
        else await call(api.POST("/api/tasks/{task_id}/unblock", { params: { path } }));
      } catch (err) {
        setError((err as Error).message);
      }
    }, kind);
  }

  const titlebar = (
    <header className="panel-titlebar">
      <h2>{task ? <TaskLabel title={task.title} project={task.project} /> : "…"}</h2>
      {task && (
        <div className="panel-titlebar-actions" role="group" aria-label="Task actions">
          {(task.editable_fields ?? []).length > 0 && !editing && (
            <button type="button" onClick={() => setEditing(true)}>
              Edit
            </button>
          )}
          {task.state === "blocked" && (
            <ActionButton
              pending={action.is("unblock")}
              disabled={action.busy}
              action={() => act("unblock")}
            >
              Unblock
            </ActionButton>
          )}
          {task.state === "failed" &&
            (confirmingRetry ? (
              <>
                <ActionButton
                  className="primary"
                  pending={action.is("retry")}
                  disabled={action.busy}
                  action={() => act("retry")}
                >
                  Confirm retry
                </ActionButton>
                <button
                  type="button"
                  disabled={action.busy}
                  onClick={() => setConfirmingRetry(false)}
                >
                  Keep failed
                </button>
              </>
            ) : (
              <button
                type="button"
                title={`Reset attempts (${task.attempt}/${task.max_attempts} used) and run it again`}
                disabled={action.busy}
                onClick={() => setConfirmingRetry(true)}
              >
                Retry
              </button>
            ))}
          {ARCHIVABLE.includes(task.state) &&
            (task.archived_at ? (
              <ActionButton
                pending={action.is("unarchive")}
                disabled={action.busy}
                action={() => act("unarchive")}
              >
                Unarchive
              </ActionButton>
            ) : (
              <ActionButton
                pending={action.is("archive")}
                disabled={action.busy}
                action={() => act("archive")}
              >
                Archive
              </ActionButton>
            ))}
          {steerable && selected === task.attempt && supportsSteer !== false && (
            <button
              type="button"
              aria-expanded={steerOpen}
              onClick={() => setSteerOpen((open) => !open)}
            >
              {steerOpen ? "Close steer" : "Steer"}
            </button>
          )}
          {!ARCHIVABLE.includes(task.state) && (
            <ActionButton
              className="danger"
              pending={action.is("cancel")}
              disabled={action.busy}
              action={() => act("cancel")}
            >
              Cancel task
            </ActionButton>
          )}
        </div>
      )}
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
          <AttemptStrip
            attempts={attemptNumbers}
            current={current}
            selected={selected}
            onSelect={setPicked}
          />
          {unknownSteers > 0 && (
            <p className="muted small" role="note">
              {unknownSteers === 1 ? "1 steer" : `${unknownSteers} steers`} to attempt {selected}{" "}
              unknown: may have been delivered (never acknowledged)
            </p>
          )}
          <Activity task={task} events={events ?? []} selected={selected} />
          {delivery && <TaskDelivery delivery={delivery} onChanged={reload} />}
          <dl className="facts task-facts">
            <dt>State</dt>
            <dd className="task-state-line">
              <span className={`badge state-${task.state}`}>{task.state.replace("_", " ")}</span>
              {task.block_reason && (
                <span className="muted"> · {blockReasonText(task.block_reason)}</span>
              )}
              {task.oom_kill && <span className="muted"> · OOM-killed (kernel out of memory)</span>}
            </dd>
            {task.kind !== "generic" && (
              <>
                <dt>Kind</dt>
                <dd>{task.kind}</dd>
              </>
            )}
            <dt>Difficulty</dt>
            <dd>
              <span className={`badge difficulty-${task.difficulty ?? "standard"}`}>
                {task.difficulty ?? "standard"}
              </span>{" "}
              <span className="muted small">({task.difficulty_source ?? "inferred"})</span>
            </dd>
            <dt>Deliverable</dt>
            <dd>{deliverableLabel(task.deliverable)}</dd>
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
                  <WorkerChip agentId={task.assignee_agent_id} layout="inline" /> · lease{" "}
                  <DateChip at={task.lease_expires_at} />
                </dd>
              </>
            )}
            {task.timeout_at && (
              <>
                <dt>Times out</dt>
                <dd>
                  <DateChip at={task.timeout_at} />
                </dd>
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
              <DateChip at={task.created_at} /> by {task.created_by}
            </dd>
          </dl>
          {!!modelTrace?.executions.length && (
            <section className="task-models" aria-label="Models">
              <div className="task-models-list">
                {modelAttempts.map((attempt) => (
                  <section
                    key={attempt}
                    className="task-models-attempt"
                    aria-label={`Attempt ${attempt} models`}
                  >
                    <span className="task-models-attempt-tag" title={`Attempt ${attempt}`}>
                      #{attempt}
                    </span>
                    <div className="task-models-runs">
                      {modelTrace.executions
                        .filter((run) => run.attempt === attempt)
                        .map((run) => (
                          <p key={run.id} className="task-models-run">
                            <span className="task-models-engine">
                              {run.layer === "harness"
                                ? (run.engine ?? "harness")
                                : (run.executor ?? "executor")}
                            </span>{" "}
                            <span className="task-models-detail">
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
                                  : ""}
                              {" · "}
                              {run.state}
                              {run.started_at ? (
                                <>
                                  {" · "}
                                  <DateChip at={run.started_at} />
                                </>
                              ) : null}
                              {run.ended_at ? (
                                <>
                                  {" → "}
                                  <DateChip at={run.ended_at} />
                                </>
                              ) : null}
                              {run.usage
                                ? ` · ${run.usage.input_tokens ?? 0}+${run.usage.output_tokens ?? 0} tokens · $${run.usage.cost_usd ?? "?"}`
                                : ""}
                            </span>
                          </p>
                        ))}
                      {modelTrace.timeline
                        .filter((selection) => selection.attempt === attempt)
                        .map((selection) => (
                          <p key={selection.id} className="task-models-hop muted">
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
                    </div>
                  </section>
                ))}
              </div>
            </section>
          )}
          {steerable && selected === task.attempt && supportsSteer !== false && (
            <TaskSteers
              taskId={taskId}
              attempt={task.attempt}
              claimed
              assigneeId={assigneeId}
              supportsSteer={supportsSteer}
              composerOpen={steerOpen}
              onComposerOpenChange={setSteerOpen}
              showList={false}
            />
          )}
          {error && <p className="error">{error}</p>}
          {task.error && <LastError key={task.error} text={task.error} />}
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
              <header className="task-section-label">Spec</header>
              <div className="spec">
                <Markdown>{task.spec || "_No spec._"}</Markdown>
              </div>
            </>
          )}
          {task.attempt > 0 && (
            <TaskTerminal
              key={`terminal-${selected}`}
              taskId={taskId}
              attempt={selected}
              changesAttempt={task.attempt}
              showChanges={task.required_capabilities.includes("repository:write")}
              changesSelected={selected}
              steer={
                selected === task.attempt
                  ? { claimed: task.state === "claimed", assigneeId, supportsSteer }
                  : undefined
              }
            />
          )}
          {task.result && (
            <>
              <header className="task-section-label">Result</header>
              <Markdown>{task.result.summary}</Markdown>
              {task.result.data && <pre>{JSON.stringify(task.result.data, null, 2)}</pre>}
            </>
          )}
          {artifacts && artifacts.length > 0 && (
            <>
              <header className="task-section-label">Artifacts</header>
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
          <header className="task-section-label">History</header>
          {events?.length === HISTORY_LIMIT && (
            <p className="muted small">
              Only the newest {HISTORY_LIMIT} events are shown. Older events are not listed.
            </p>
          )}
          <EventList events={events ?? []} />
        </>
      )}
    </>
  );

  if (variant === "panel") {
    return (
      <section className="task-panel" aria-label="Task detail">
        {titlebar}
        <div className="panel-body">{body}</div>
      </section>
    );
  }

  return (
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
  );
}
