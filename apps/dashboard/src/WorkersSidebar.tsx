import { useState } from "react";
import { type Agent, api, call } from "./api";
import { useLiveData } from "./live";
import { useNow } from "./views/Activity";
import { WorkerChip } from "./WorkerChip";

const ENDED_LIMIT = 8;
const RAIL_KEY = "argus.workers-rail";

function storedCollapsed(): boolean {
  try {
    return localStorage.getItem(RAIL_KEY) === "collapsed";
  } catch {
    return false;
  }
}

function rememberCollapsed(collapsed: boolean): void {
  try {
    localStorage.setItem(RAIL_KEY, collapsed ? "collapsed" : "open");
  } catch {
    // Private mode or a full store: the toggle still works for this view.
  }
}

function liveWorkers(agents: Agent[] | undefined): Agent[] {
  return (agents ?? []).filter(
    (a) => a.role === "worker" && (a.state === "active" || a.state === "draining"),
  );
}

/** Newest ended workers (lost/gone), so a quiet hour does not empty the rail. */
function recentlyEnded(agents: Agent[] | undefined): Agent[] {
  return (agents ?? [])
    .filter(
      (a) =>
        a.role === "worker" && (a.state === "lost" || a.state === "gone") && a.ended_at != null,
    )
    .sort((a, b) => new Date(b.ended_at!).getTime() - new Date(a.ended_at!).getTime())
    .slice(0, ENDED_LIMIT);
}

function RailChevron({ collapsed }: { collapsed: boolean }) {
  return (
    <svg viewBox="0 0 16 16" width={14} height={14} aria-hidden="true">
      <path
        d={collapsed ? "M10 3 5 8l5 5" : "M6 3l5 5-5 5"}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Full-height rail of live workers; selection drives the main worker panel. */
export function WorkersSidebar({ selectedId }: { selectedId: string | null }) {
  const now = useNow(5000);
  const [collapsed, setCollapsed] = useState(storedCollapsed);
  const { data: agents, error } = useLiveData(
    (signal) => call(api.GET("/api/agents", { signal })),
    [],
  );
  const workers = liveWorkers(agents);
  const ended = recentlyEnded(agents);
  const busy = workers.filter((a) => a.claimed_tasks > 0).length;
  const quiet = workers.filter((a) => a.quiet_since).length;
  const listed = [...workers, ...ended];
  const summary =
    agents === undefined
      ? ""
      : `${workers.length} live${busy > 0 ? ` · ${busy} busy` : ""}${
          quiet > 0 ? ` · ${quiet} quiet` : ""
        }${ended.length > 0 ? ` · ${ended.length} ended` : ""}`;

  function toggleCollapsed() {
    setCollapsed((value) => {
      rememberCollapsed(!value);
      return !value;
    });
  }

  return (
    <aside className={`workers-rail${collapsed ? " collapsed" : ""}`} aria-label="Running workers">
      <header className="workers-rail-head">
        <div className="rail-title">
          {!collapsed && <p className="workers-rail-label">Workers</p>}
          {!collapsed && <a href="#workers/agents">View all</a>}
          <button
            type="button"
            className="workers-rail-toggle"
            aria-expanded={!collapsed}
            aria-controls="workers-rail-list"
            aria-label={collapsed ? "Expand workers" : "Collapse workers"}
            title={collapsed ? "Expand workers" : "Collapse workers"}
            onClick={toggleCollapsed}
          >
            <RailChevron collapsed={collapsed} />
          </button>
        </div>
        {!collapsed && summary && <p className="workers-rail-summary muted small">{summary}</p>}
      </header>
      {collapsed && workers.length > 0 && (
        <ul className="workers-rail-icons">
          {workers.map((a) => (
            <li key={a.id}>
              <WorkerChip agentId={a.id} agent={a} layout="icon" selected={a.id === selectedId} />
            </li>
          ))}
        </ul>
      )}
      <div id="workers-rail-list" hidden={collapsed}>
        {agents === undefined ? (
          <p className="workers-rail-empty muted small">{error ? "—" : "…"}</p>
        ) : listed.length === 0 ? (
          <p className="workers-rail-empty muted small">None live</p>
        ) : (
          <ul className="workers-list">
            {listed.map((a) => (
              <li key={a.id}>
                <WorkerChip
                  agentId={a.id}
                  agent={a}
                  layout="rail"
                  now={now}
                  selected={a.id === selectedId}
                />
              </li>
            ))}
          </ul>
        )}
      </div>
    </aside>
  );
}
