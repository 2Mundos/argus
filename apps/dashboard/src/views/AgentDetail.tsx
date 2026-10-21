import { useEffect, useRef, useState } from "react";
import { api, call, fetchText } from "../api";
import { relative, shortId } from "../format";
import { useLiveData } from "../live";
import { TaskLabel } from "../TaskLabel";
import { Activity, useNow } from "./Activity";

const LOG_TAIL = 200;
const LOG_EVERY_MS = 5000;

/** An agent, the task it holds and its container output (live work spec 3.2). */
export function AgentDetail({ agentId, onClose }: { agentId: string; onClose: () => void }) {
  const now = useNow();
  const { data } = useLiveData(
    async (signal) => {
      const [agent, page] = await Promise.all([
        call(
          api.GET("/api/agents/{agent_id}", { params: { path: { agent_id: agentId } }, signal }),
        ),
        call(
          api.GET("/api/tasks", {
            params: { query: { assignee: agentId, state: ["claimed"], view: "detail" } },
            signal,
          }),
        ),
      ]);
      const work = await Promise.all(
        page.items.flatMap((task) =>
          "progress_at" in task
            ? [
                call(
                  api.GET("/api/events", {
                    params: { query: { subject_id: task.id, limit: 50 } },
                    signal,
                  }),
                ).then((events) => ({ task, events })),
              ]
            : [],
        ),
      );
      return { agent, work };
    },
    [agentId],
  );
  const agent = data?.agent;

  return (
    <div className="overlay" onClick={onClose} onKeyDown={(e) => e.key === "Escape" && onClose()}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()} onKeyDown={() => {}}>
        <div className="toolbar">
          <h2>{agent?.name ?? "…"}</h2>
          <button type="button" className="link" onClick={onClose}>
            Close
          </button>
        </div>
        {agent && (
          <>
            <dl className="facts">
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
            <h3>Current work</h3>
            {data.work.length === 0 && <p className="muted">Holds no task.</p>}
            {data.work.map(({ task, events }) => (
              <div key={task.id}>
                <p>
                  <strong>
                    <TaskLabel title={task.title} project={task.project} />
                  </strong>{" "}
                  <span className="muted small">
                    {shortId(task.id)} · attempt {task.attempt}
                  </span>
                </p>
                <Activity task={task} events={events} />
              </div>
            ))}
            <h3>Container output</h3>
            {agent.instance_id ? (
              <LogTail instanceId={agent.instance_id} tail={LOG_TAIL} />
            ) : (
              <p className="muted">
                This agent was not started from a pool, so Argus cannot read its output.
              </p>
            )}
          </>
        )}
      </aside>
    </div>
  );
}

/** The instance's last lines, re-read every 5 s until paused or unmounted. */
export function LogTail({ instanceId, tail }: { instanceId: string; tail: number }) {
  const [text, setText] = useState<string>();
  const [error, setError] = useState<string>();
  const [paused, setPaused] = useState(false);
  const pre = useRef<HTMLPreElement>(null);
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
    if (pre.current && atEnd.current) pre.current.scrollTop = pre.current.scrollHeight;
  }, [text]);

  return (
    <>
      <div className="actions left">
        <button type="button" onClick={() => setPaused((p) => !p)}>
          {paused ? "Resume" : "Pause"}
        </button>
        <span className="muted small">
          last {tail} lines{paused ? ", paused" : `, every ${LOG_EVERY_MS / 1000} s`}
        </span>
      </div>
      {error && <p className="error">{error}</p>}
      <pre
        ref={pre}
        className="logs"
        onScroll={(e) => {
          const el = e.currentTarget;
          atEnd.current = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
        }}
      >
        {text || (text === undefined ? "Loading…" : "No output.")}
      </pre>
    </>
  );
}
