import { type Agent, api, call } from "./api";
import { useLiveData } from "./live";
import { WorkerChip } from "./WorkerChip";
import { useNow } from "./views/Activity";

const ENDED_LIMIT = 8;

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

/** Full-height rail of live workers; selection drives the main worker panel. */
export function WorkersSidebar({ selectedId }: { selectedId: string | null }) {
  const now = useNow(5000);
  const { data: agents, error } = useLiveData(
    (signal) => call(api.GET("/api/agents", { signal })),
    [],
  );
  const workers = liveWorkers(agents);
  const ended = recentlyEnded(agents);
  const busy = workers.filter((a) => a.claimed_tasks > 0).length;
  const quiet = workers.filter((a) => a.quiet_since).length;
  const listed = [...workers, ...ended];

  return (
    <aside className="workers-rail" aria-label="Running workers">
      <header className="workers-rail-head">
        <div className="rail-title">
          <p className="workers-rail-label">Workers</p>
          <a href="#workers/agents">View all</a>
        </div>
        {agents !== undefined && (
          <p className="workers-rail-summary muted small">
            {workers.length} live
            {busy > 0 ? ` · ${busy} busy` : ""}
            {quiet > 0 ? ` · ${quiet} quiet` : ""}
            {ended.length > 0 ? ` · ${ended.length} ended` : ""}
          </p>
        )}
      </header>
      {agents === undefined ? (
        <p className="workers-rail-empty muted small">{error ? "—" : "…"}</p>
      ) : listed.length === 0 ? (
        <p className="workers-rail-empty muted small">None live</p>
      ) : (
        <ul className="workers-list">
          {listed.map((a) => (
            <li key={a.id}>
              <WorkerChip agentId={a.id} agent={a} layout="rail" now={now} selected={a.id === selectedId} />
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
