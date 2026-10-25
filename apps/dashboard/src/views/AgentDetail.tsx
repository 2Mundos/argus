import { type PointerEvent as ReactPointerEvent, useEffect, useRef, useState } from "react";
import { api, call, fetchText } from "../api";
import { relative, shortId } from "../format";
import { useLiveData } from "../live";
import { TaskLabel } from "../TaskLabel";
import { renderTerminalText } from "../terminalLog";
import { useNow } from "./Activity";
import { TaskLog } from "./TaskLog";

const LOG_TAIL = 200;
const LOG_EVERY_MS = 2000;
const META_PCT_MIN = 12;
const META_PCT_MAX = 70;
const META_PCT_DEFAULT = 28;

/**
 * An agent, the task it holds, and its live stream (live work spec 3.2).
 * `panel` fills the main column (sidebar stays); `drawer` is the modal overlay.
 */
export function AgentDetail({
  agentId,
  onClose,
  variant = "drawer",
}: {
  agentId: string;
  onClose: () => void;
  variant?: "drawer" | "panel";
}) {
  const now = useNow();
  const panelRef = useRef<HTMLElement>(null);
  const [metaPct, setMetaPct] = useState(META_PCT_DEFAULT);
  const { data } = useLiveData(
    async (signal) => {
      const [agent, models, page] = await Promise.all([
        call(
          api.GET("/api/agents/{agent_id}", { params: { path: { agent_id: agentId } }, signal }),
        ),
        call(
          api.GET("/api/agents/{agent_id}/models", {
            params: { path: { agent_id: agentId } },
            signal,
          }),
        ),
        call(
          api.GET("/api/tasks", {
            params: { query: { assignee: agentId, state: ["claimed"], view: "detail" } },
            signal,
          }),
        ),
      ]);
      return {
        agent,
        models,
        work: page.items.filter((task) => "progress_at" in task),
      };
    },
    [agentId],
  );
  const agent = data?.agent;

  useEffect(() => {
    if (variant !== "panel") return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [variant, onClose]);

  function onSplitPointerDown(e: ReactPointerEvent<HTMLHRElement>) {
    if (e.button !== 0) return;
    const panel = panelRef.current;
    const metaEl = panel?.querySelector(".worker-panel-meta");
    if (!panel || !metaEl) return;
    e.preventDefault();
    const handle = e.currentTarget;
    handle.setPointerCapture(e.pointerId);
    const top = metaEl.getBoundingClientRect().top;
    const total = panel.getBoundingClientRect().bottom - top;
    const prevCursor = document.body.style.cursor;
    const prevSelect = document.body.style.userSelect;
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";

    const onMove = (ev: PointerEvent) => {
      if (total <= 0) return;
      const pct = ((ev.clientY - top) / total) * 100;
      setMetaPct(Math.min(META_PCT_MAX, Math.max(META_PCT_MIN, pct)));
    };
    const onUp = (ev: PointerEvent) => {
      handle.releasePointerCapture(ev.pointerId);
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      document.body.style.cursor = prevCursor;
      document.body.style.userSelect = prevSelect;
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
  }

  const close = (
    <button type="button" className="icon-close" aria-label="Close" onClick={onClose}>
      <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
        <path
          d="M3.2 3.2a.75.75 0 0 1 1.06 0L8 6.94l3.74-3.74a.75.75 0 1 1 1.06 1.06L9.06 8l3.74 3.74a.75.75 0 1 1-1.06 1.06L8 9.06l-3.74 3.74a.75.75 0 1 1-1.06-1.06L6.94 8 3.2 4.26a.75.75 0 0 1 0-1.06Z"
          fill="currentColor"
        />
      </svg>
    </button>
  );

  const meta = agent && (
    <>
      <dl className="facts">
        <dt>Name</dt>
        <dd>{agent.name}</dd>
        <dt>Role</dt>
        <dd>{agent.role}</dd>
        <dt>State</dt>
        <dd>
          <span className={`badge agent-${agent.state}`}>{agent.state}</span>
          {agent.end_reason && <span className="muted small"> {agent.end_reason}</span>}
        </dd>
        <dt>Heartbeat</dt>
        <dd>{relative(agent.last_heartbeat_at, now)}</dd>
        {agent.runtime && (
          <>
            <dt>Runtime</dt>
            <dd>
              {[agent.runtime.worker_version, agent.runtime.engine, agent.runtime.executor]
                .map((v) => v ?? "unknown")
                .join(" · ")}
            </dd>
          </>
        )}
        <dt>Capabilities</dt>
        <dd>{agent.capabilities.join(", ") || "—"}</dd>
        <dt>Instance</dt>
        <dd>{agent.instance_id ? shortId(agent.instance_id) : "none"}</dd>
      </dl>
      <h3>Models</h3>
      {data?.models && Object.keys(data.models.current).length ? (
        <dl className="facts" aria-label="Current models">
          {Object.entries(data.models.current).map(([layer, run]) => (
            <div key={layer}>
              <dt>{layer}</dt>
              <dd>
                {run.executor ?? run.engine ?? "unknown"} ·{" "}
                {run.observed_model ?? run.configured_model ?? run.requested_model ?? "?"}
                {run.observed_model
                  ? ` (${run.evidence})`
                  : run.configured_model
                    ? " (configured)"
                    : ""}
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="muted">No model reports.</p>
      )}
      {data?.models?.history?.length ? (
        <div className="plain" aria-label="Recent model runs">
          {data.models.history.slice(0, 8).map((run) => (
            <p key={run.id} className="muted small">
              {run.layer} · {run.executor ?? run.engine ?? "unknown"} ·{" "}
              {run.observed_model ?? run.configured_model ?? run.requested_model ?? "?"} ·{" "}
              {run.state}
            </p>
          ))}
        </div>
      ) : null}
      <h3>Current work</h3>
      <table className="work-table">
        <thead>
          <tr>
            <th>Task</th>
            <th>Attempt</th>
            <th>Progress</th>
            <th>Note</th>
            <th>Updated</th>
          </tr>
        </thead>
        <tbody>
          {data.work.length === 0 ? (
            <tr>
              <td colSpan={5} className="muted">
                Holds no task.
              </td>
            </tr>
          ) : (
            data.work.map((task) => (
              <tr key={task.id}>
                <td>
                  <TaskLabel title={task.title} project={task.project} />{" "}
                  <span className="muted small">{shortId(task.id)}</span>
                </td>
                <td>{task.attempt}</td>
                <td>{task.progress_percent != null ? `${task.progress_percent}%` : "—"}</td>
                <td>{task.progress_message ?? "—"}</td>
                <td className="muted small">
                  {task.progress_at ? `updated ${relative(task.progress_at, now)}` : "silent"}
                  {task.state === "claimed" && ` · lease ${relative(task.lease_expires_at, now)}`}
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </>
  );

  // Prefer the task live log (NanoClaw/delegate stream). Container stdout is only
  // the worker process lifecycle, and only when no claimed task is held.
  const held = data?.work[0];
  const output =
    agent &&
    (held && held.attempt > 0 ? (
      <TaskLog
        taskId={held.id}
        attempt={held.attempt}
        steer={{
          claimed: held.state === "claimed",
          assigneeId: held.assignee_agent_id ?? agent.id,
          supportsSteer: agent.runtime?.features?.includes("steer") ?? false,
        }}
      />
    ) : agent.instance_id ? (
      <LogTail instanceId={agent.instance_id} tail={LOG_TAIL} />
    ) : (
      <p className="muted">
        This agent was not started from a pool, so Argus cannot read its output.
      </p>
    ));

  if (variant === "panel") {
    return (
      <section className="worker-panel" ref={panelRef}>
        {close}
        {meta && (
          <>
            <div className="worker-panel-meta" style={{ flex: `0 0 ${metaPct}%` }}>
              {meta}
            </div>
            <hr
              className="worker-split"
              role="separator"
              aria-orientation="horizontal"
              aria-valuenow={Math.round(metaPct)}
              aria-valuemin={META_PCT_MIN}
              aria-valuemax={META_PCT_MAX}
              onPointerDown={onSplitPointerDown}
            />
          </>
        )}
        {output && <div className="worker-panel-output">{output}</div>}
      </section>
    );
  }

  return (
    <div className="overlay" onClick={onClose} onKeyDown={(e) => e.key === "Escape" && onClose()}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()} onKeyDown={() => {}}>
        {close}
        {meta}
        {output}
      </aside>
    </div>
  );
}

/** The instance's last lines as a live terminal; re-read until paused or unmounted. */
export function LogTail({ instanceId, tail }: { instanceId: string; tail: number }) {
  const [text, setText] = useState<string>();
  const [error, setError] = useState<string>();
  const [paused, setPaused] = useState(false);
  const [tick, setTick] = useState(0);
  const body = useRef<HTMLPreElement>(null);
  const atEnd = useRef(true);

  useEffect(() => {
    if (paused) return;
    let cancelled = false;
    const read = () =>
      fetchText(`/api/instances/${instanceId}/logs?tail=${tail}`).then(
        (value) => {
          if (cancelled) return;
          setText(value);
          setError(undefined);
          setTick((n) => n + 1);
        },
        (err: Error) => {
          if (!cancelled) setError(err.message);
        },
      );
    void read();
    const timer = setInterval(read, LOG_EVERY_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [instanceId, tail, paused]);

  useEffect(() => {
    // Keep following the end unless the reader scrolled up.
    if (body.current && atEnd.current) body.current.scrollTop = body.current.scrollHeight;
  }, [text]);

  return (
    <div className={`term${paused ? " term-paused" : ""}`}>
      <div className="term-bar">
        <span className="term-traffic" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        <span className="term-title">
          container · {shortId(instanceId)} · last {tail}
        </span>
        <span className={`term-pulse${paused ? "" : " on"}`} title={paused ? "Paused" : "Live"}>
          {paused ? "paused" : "live"}
        </span>
        <button type="button" className="term-action" onClick={() => setPaused((p) => !p)}>
          {paused ? "Resume" : "Pause"}
        </button>
      </div>
      {error && <p className="term-error">{error}</p>}
      <pre
        ref={body}
        className="term-body logs"
        data-tick={tick}
        onScroll={(e) => {
          const el = e.currentTarget;
          atEnd.current = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
        }}
      >
        {text === undefined ? "Connecting…" : text ? renderTerminalText(text) : "No output yet."}
      </pre>
    </div>
  );
}
