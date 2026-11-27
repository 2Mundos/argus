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
import { TaskReadiness } from "../readiness";
import { TaskLabel } from "../TaskLabel";
import { TaskStateBadge } from "../TaskStateBadge";
import { pinText } from "../taskEdit";
import { WorkerChip } from "../WorkerChip";
import { Activity } from "./Activity";
import { AttemptStrip } from "./AttemptStrip";
import { BudgetBlockAction } from "./BudgetBlockAction";
import { EventList } from "./EventList";
import { RepositoryBlockAction } from "./RepositoryBlockAction";
import { TaskContinuation } from "./TaskContinuation";
import { TaskCostTile } from "./TaskCost";
import { TaskEditForm } from "./TaskEditForm";
import { TaskIntegrations } from "./TaskIntegrations";
import { TaskPrerequisites } from "./TaskPrerequisites";
import { TaskReviews } from "./TaskReviews";
import { TaskSteers, useSteersSource } from "./TaskSteers";
import { TaskTerminal } from "./TaskTerminal";

/** Size of the History page; a full page may hide older events. */
const HISTORY_LIMIT = 50;
const ERROR_EXCERPT = 160;

const CHECKPOINT_TEXT = {
  available: "work can be restored",
  unavailable: "work cannot be restored or inspected",
  unknown: "checkpoint unknown: none recorded yet, still running, or state not recognised",
} as const;

const OWNER_STATUS_TEXT = {
  live: "owner is live",
  stale: "owner went silent; it may no longer be running",
  ended: "owner has ended",
  unknown: "owner may no longer be running",
} as const;

function LastError({ text, label, previous }: { text: string; label: string; previous: boolean }) {
  const [open, setOpen] = useState(false);
  const needsMore = text.length > ERROR_EXCERPT;
  const shown = open || !needsMore ? text : `${text.slice(0, ERROR_EXCERPT).trimEnd()}…`;
  return (
    <p className={previous ? "muted last-error previous-error" : "error last-error"}>
      {label}: {shown}
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

// A requeued or re-claimed task still carries its last failure; say whose it is.
function errorLabel(task: {
  error_attempt?: number | null;
  error_scope?: "current" | "previous" | null;
}): string {
  const attempt = task.error_attempt ? ` (attempt ${task.error_attempt})` : "";
  return task.error_scope === "previous"
    ? `Previous attempt's error${attempt}`
    : `Last error${attempt}`;
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
  const [forcing, setForcing] = useState(false);
  const [forceReason, setForceReason] = useState("");
  const [editing, setEditing] = useState(false);
  // The attempt the user picked, kept with the task and current attempt it was picked
  // under: a new current attempt or another task re-selects the current one. No effect
  // resets it, so a click right after the panel loads is never overwritten.
  const [pick, setPick] = useState<{ taskId: string; current: number; attempt: number } | null>(
    null,
  );
  const [steerOpen, setSteerOpen] = useState(false);
  // The task itself paints the panel; every other section fills in on its own, so a
  // slow forge read (delivery) or a large read (models) never holds the whole view.
  const { data: task, reload: reloadTask } = useLiveData(
    (signal) =>
      call(api.GET("/api/tasks/{task_id}", { params: { path: { task_id: taskId } }, signal })),
    [taskId],
  );
  const { data: executionRoute } = useLiveData(
    (signal) =>
      call(
        api.GET("/api/tasks/{task_id}/route", { params: { path: { task_id: taskId } }, signal }),
      ).catch(() => null),
    [taskId],
  );
  const { data: artifacts, reload: reloadArtifacts } = useLiveData(
    (signal) =>
      call(
        api.GET("/api/tasks/{task_id}/artifacts", {
          params: { path: { task_id: taskId } },
          signal,
        }),
      ),
    [taskId],
  );
  const { data: events, reload: reloadEvents } = useLiveData(
    (signal) =>
      call(
        api.GET("/api/events", {
          params: { query: { subject_id: taskId, limit: HISTORY_LIMIT } },
          signal,
        }),
      ),
    [taskId],
  );
  // A forge hiccup must not take the drawer down: no delivery section instead.
  const { data: delivery, reload: reloadDelivery } = useLiveData(
    (signal) =>
      call(
        api.GET("/api/tasks/{task_id}/delivery", {
          params: { path: { task_id: taskId } },
          signal,
        }),
      ).catch(() => null),
    [taskId],
  );
  const { data: modelTrace, reload: reloadModels } = useLiveData(
    (signal) =>
      call(
        api.GET("/api/tasks/{task_id}/models", {
          params: { path: { task_id: taskId } },
          signal,
        }),
      ),
    [taskId],
  );
  // The strip is an aid: without it the current attempt stays selected. The summary
  // omits the stored definitions, which this view never renders.
  const { data: attemptList, reload: reloadAttempts } = useLiveData(
    (signal) =>
      call(
        api.GET("/api/tasks/{task_id}/attempts/summary", {
          params: { path: { task_id: taskId } },
          signal,
        }),
      ).catch(() => []),
    [taskId],
  );
  const reload = () => {
    reloadTask();
    reloadArtifacts();
    reloadEvents();
    reloadDelivery();
    reloadModels();
    reloadAttempts();
  };
  const current = task?.attempt ?? 0;
  const prevTaskId = useRef(taskId);
  const prevCurrent = useRef<number | null>(null);
  // A new current attempt (or another task) closes the steer composer.
  useEffect(() => {
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
  const picked = pick?.taskId === taskId && pick.current === current ? pick.attempt : null;
  const selected = picked !== null && attemptNumbers.includes(picked) ? picked : current;
  // One steers source feeds both the actions row and the terminal; the assignee is
  // looked up once here rather than by each copy.
  const steers = useSteersSource({
    taskId,
    attempt: current,
    claimed: steerable,
    assigneeId,
    enabled: steerable && selected === current,
  });
  // Unknown until the assignee loads; a failed lookup counts as unsupported.
  const supportsSteer = !steerable || !steers.known ? undefined : steers.can;
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

  async function forceAccept() {
    const reason = forceReason.trim();
    if (!reason) return;
    setError(undefined);
    await action.run(async () => {
      try {
        await call(
          api.POST("/api/tasks/{task_id}/force-accept", {
            params: { path: { task_id: taskId } },
            body: { reason, expected_version: task?.version },
          }),
        );
        setForcing(false);
        setForceReason("");
      } catch (err) {
        setError((err as Error).message);
      }
    }, "force_accept");
  }

  const titlebar = (
    <header className="panel-titlebar">
      <h2>{task ? <TaskLabel title={task.title} project={task.project} /> : "…"}</h2>
      {task && (
        <div className="panel-titlebar-actions" role="group" aria-label="Task actions">
          {(task.editable_fields ?? []).length > 0 && !editing && (
            <button type="button" className="compact" onClick={() => setEditing(true)}>
              Edit
            </button>
          )}
          {task.state === "blocked" && task.block_reason !== "budget_exhausted" && (
            <ActionButton
              className="compact"
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
                  className="primary compact"
                  pending={action.is("retry")}
                  disabled={action.busy}
                  action={() => act("retry")}
                >
                  Confirm retry
                </ActionButton>
                <button
                  type="button"
                  className="compact"
                  disabled={action.busy}
                  onClick={() => setConfirmingRetry(false)}
                >
                  Keep failed
                </button>
              </>
            ) : (
              <button
                type="button"
                className="compact"
                title={`Reset failures (${task.failures}/${task.max_attempts} used) and run it again`}
                disabled={action.busy}
                onClick={() => setConfirmingRetry(true)}
              >
                Retry
              </button>
            ))}
          {ARCHIVABLE.includes(task.state) &&
            (task.archived_at ? (
              <ActionButton
                className="compact"
                pending={action.is("unarchive")}
                disabled={action.busy}
                action={() => act("unarchive")}
              >
                Unarchive
              </ActionButton>
            ) : (
              <ActionButton
                className="compact"
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
              className="compact"
              aria-expanded={steerOpen}
              onClick={() => setSteerOpen((open) => !open)}
            >
              {steerOpen ? "Close steer" : "Steer"}
            </button>
          )}
          {!ARCHIVABLE.includes(task.state) && (
            <button
              type="button"
              className="compact"
              disabled={action.busy}
              onClick={() => setForcing(true)}
            >
              Accept and close
            </button>
          )}
          {!ARCHIVABLE.includes(task.state) && (
            <ActionButton
              className="danger compact"
              pending={action.is("cancel")}
              disabled={action.busy}
              action={() => act("cancel")}
            >
              Cancel task
            </ActionButton>
          )}
        </div>
      )}
      {task && forcing && (
        <div
          role="dialog"
          aria-label="Accept and close"
          className="force-accept-dialog"
          onClick={(e) => e.stopPropagation()}
          onKeyDown={() => {}}
        >
          <p>
            Close this task as accepted without review. It ends as succeeded, recorded as a forced
            owner acceptance; nothing is merged or changed on the forge.
          </p>
          <label>
            Reason (required)
            <textarea
              value={forceReason}
              maxLength={2000}
              rows={3}
              onChange={(e) => setForceReason(e.target.value)}
            />
          </label>
          <div className="actions">
            <ActionButton
              className="primary"
              pending={action.is("force_accept")}
              disabled={action.busy || !forceReason.trim()}
              action={forceAccept}
            >
              Confirm accept and close
            </ActionButton>
            <button
              type="button"
              disabled={action.busy}
              onClick={() => {
                setForcing(false);
                setForceReason("");
              }}
            >
              Keep open
            </button>
          </div>
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
            onSelect={(attempt) => setPick({ taskId, current, attempt })}
          />
          {unknownSteers > 0 && (
            <p className="muted small" role="note">
              {unknownSteers === 1 ? "1 steer" : `${unknownSteers} steers`} to attempt {selected}{" "}
              unknown: may have been delivered (never acknowledged)
            </p>
          )}
          <div className="task-status-band">
            <Activity task={task} events={events ?? []} selected={selected} />
            <div className="task-live">
              {delivery && <TaskDelivery delivery={delivery} onChanged={reload} />}
              <TaskReadiness task={task} />
              {task.kind === "code_review" && task.parent_id && (
                <p className="small task-parent-link">
                  Review round of <a href={`#tasks/${task.parent_id}`}>parent task</a>
                </p>
              )}
            </div>
          </div>
          {task.state === "blocked" && task.block_reason === "env_repository_unconfigured" && (
            <RepositoryBlockAction taskId={taskId} project={task.project} onUnblocked={reload} />
          )}
          {task.state === "blocked" && task.block_reason === "budget_exhausted" && (
            <BudgetBlockAction task={task} onChanged={reload} />
          )}
          {steerable && selected === task.attempt && supportsSteer !== false && (
            <TaskSteers
              taskId={taskId}
              attempt={task.attempt}
              claimed
              assigneeId={assigneeId}
              supportsSteer={supportsSteer}
              source={steers}
              composerOpen={steerOpen}
              onComposerOpenChange={setSteerOpen}
              showList={false}
            />
          )}
          {error && <p className="error task-inline-error">{error}</p>}
          {task.error && (
            <LastError
              key={`${task.error_attempt}:${task.error}`}
              text={task.error}
              previous={task.error_scope === "previous"}
              label={errorLabel(task)}
            />
          )}
          {task.kind !== "code_review" && <TaskReviews taskId={taskId} />}
          <div className="task-meta-card">
            <dl className="facts task-facts">
              <div
                className={
                  task.block_reason || task.oom_kill ? "task-fact task-fact-wide" : "task-fact"
                }
              >
                <dt>State</dt>
                <dd className="task-state-line">
                  <TaskStateBadge state={task.state} />
                  {task.block_reason && (
                    <span className="muted"> · {blockReasonText(task.block_reason)}</span>
                  )}
                  {task.oom_kill && (
                    <span className="muted"> · OOM-killed (kernel out of memory)</span>
                  )}
                </dd>
              </div>
              {task.kind !== "generic" && (
                <div className="task-fact">
                  <dt>Kind</dt>
                  <dd>{task.kind}</dd>
                </div>
              )}
              <div className="task-fact">
                <dt>Difficulty</dt>
                <dd>
                  <span className={`badge difficulty-${task.difficulty ?? "standard"}`}>
                    {task.difficulty ?? "standard"}
                  </span>{" "}
                  <span className="muted small">({task.difficulty_source ?? "inferred"})</span>
                </dd>
              </div>
              <div className="task-fact">
                <dt>Deliverable</dt>
                <dd>{deliverableLabel(task.deliverable)}</dd>
              </div>
              <div className="task-fact">
                <dt>Attempt</dt>
                <dd>
                  {task.attempt} (failures {task.failures}/{task.max_attempts})
                </dd>
              </div>
              {task.assignee_agent_id && (
                <div className="task-fact">
                  <dt>Assignee</dt>
                  <dd>
                    <WorkerChip agentId={task.assignee_agent_id} layout="inline" /> · lease{" "}
                    <DateChip at={task.lease_expires_at} />
                  </dd>
                </div>
              )}
              {task.timeout_at && (
                <div className="task-fact">
                  <dt>Times out</dt>
                  <dd>
                    <DateChip at={task.timeout_at} />
                  </dd>
                </div>
              )}
              <TaskFailover task={task} />
              {task.required_capabilities.length > 0 && (
                <div className="task-fact">
                  <dt>Needs</dt>
                  <dd>{task.required_capabilities.join(", ")}</dd>
                </div>
              )}
              {task.account_providers && (
                <div className="task-fact">
                  <dt>Runs on</dt>
                  <dd>{task.account_providers.join(", ")}</dd>
                </div>
              )}
              {task.harness_account && (
                <div className="task-fact">
                  <dt>Harness account</dt>
                  <dd>{task.harness_account}</dd>
                </div>
              )}
              {task.harness_model && (
                <div className="task-fact">
                  <dt>Harness</dt>
                  <dd>{pinText(task.harness_model)}</dd>
                </div>
              )}
              {task.executor_model && (
                <div className="task-fact">
                  <dt>Executor</dt>
                  <dd>{pinText(task.executor_model)}</dd>
                </div>
              )}
              <div className="task-fact">
                <dt>Created</dt>
                <dd>
                  <DateChip at={task.created_at} /> by {task.created_by}
                </dd>
              </div>
              {task.review_disposition && (
                <div className="task-fact task-fact-wide">
                  <dt>Review</dt>
                  <dd className="review-disposition">
                    {reviewOutcomeText(task.review_disposition.outcome)}
                    {task.review_disposition.reason && (
                      <span className="muted"> · {task.review_disposition.reason}</span>
                    )}
                    {(task.review_disposition.unmet?.length ?? 0) > 0 && (
                      <span className="muted">
                        {" "}
                        · {task.review_disposition.unmet?.length} acceptance item(s) verification
                        missing
                      </span>
                    )}
                  </dd>
                </div>
              )}
              <div className="task-fact task-fact-wide">
                <dt>Workspace</dt>
                <dd>
                  {task.workspace ? (
                    <>
                      <WorkerChip agentId={task.workspace.owner_agent_id} layout="inline" /> ·{" "}
                      {CHECKPOINT_TEXT[task.workspace.checkpoint]}
                      <span className="muted small">
                        {" "}
                        ({OWNER_STATUS_TEXT[task.workspace.owner_status ?? "unknown"]}
                        {task.workspace.owner_node_id ? ` on ${task.workspace.owner_node_id}` : ""})
                      </span>
                    </>
                  ) : (
                    <span className="muted">
                      No workspace yet: the task has not started an attempt.
                    </span>
                  )}
                </dd>
              </div>
              {task.knowledge.length > 0 && (
                <div className="task-fact task-fact-wide">
                  <dt>Pinned pages</dt>
                  <dd>{task.knowledge.map(shortId).join(", ")}</dd>
                </div>
              )}
              {task.depends_on.length > 0 && (
                <div className="task-fact task-fact-wide">
                  <dt>Depends on</dt>
                  <dd>{task.depends_on.map(shortId).join(", ")}</dd>
                </div>
              )}
            </dl>
            <TaskCostTile taskId={taskId} />
            {!!modelTrace?.executions.length && (
              <section className="task-models" aria-label="Models">
                <header className="task-section-label">Models</header>
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
                                {!run.observed_model && run.evidence_reason
                                  ? ` · not reported: ${run.evidence_reason.replaceAll("_", " ")}`
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
          </div>
          <div className="task-disclosures">
            {executionRoute?.execution && (
              <details className="task-disclosure task-route">
                <summary>
                  <span className="task-section-label">Execution route</span>
                  <span className="task-disclosure-meta muted small">
                    {executionRoute.execution.selection?.alternative.id ?? "Not selected"} · cursor{" "}
                    {executionRoute.execution.cursor}
                  </span>
                </summary>
                <div className="task-disclosure-body">
                  <p className="muted small">
                    Last recorded selection is not a claim of current eligibility. Task pins bypass
                    the whole chain; resets and classified advances are recorded in activity.
                  </p>
                  {executionRoute.execution.review && (
                    <p className="small">
                      Independent review of {executionRoute.execution.review.parent_id}: chose{" "}
                      {executionRoute.execution.review.selected}
                      {executionRoute.execution.review.skipped?.map(
                        (s) => `; skipped ${s.id} (${s.reason})`,
                      )}
                    </p>
                  )}
                  <pre>{JSON.stringify(executionRoute.execution, null, 2)}</pre>
                </div>
              </details>
            )}
            <TaskContinuation taskId={taskId} />
            {task.depends_on.length > 0 && <TaskPrerequisites taskId={taskId} />}
          </div>
          {editing ? (
            <TaskEditForm
              task={task}
              onDone={(saved) => {
                setEditing(false);
                if (saved) reload();
              }}
            />
          ) : (
            <section className="task-spec-section" aria-label="Spec">
              <header className="task-section-label">Spec</header>
              <div className="spec">
                <Markdown>{task.spec || "_No spec._"}</Markdown>
              </div>
            </section>
          )}
          {task.project !== "global" && <TaskIntegrations task={task} onSaved={reload} />}
          {task.attempt > 0 && (
            <TaskTerminal
              key={`terminal-${selected}`}
              taskId={taskId}
              attempt={selected}
              changesAttempt={task.attempt}
              showChanges={task.required_capabilities.includes("repository:write")}
              changesSelected={selected}
              steers={steers}
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

const REVIEW_OUTCOMES: Record<string, string> = {
  accepted: "accepted",
  accepted_with_deviation: "accepted with recorded deviation",
  not_reviewed: "not reviewed",
  force_accepted: "accepted and closed by the owner (forced, no review)",
};

function reviewOutcomeText(outcome: string): string {
  return REVIEW_OUTCOMES[outcome] ?? outcome;
}
