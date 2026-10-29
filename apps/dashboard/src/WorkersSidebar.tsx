import { type Agent, type ArgusEvent, api, call } from "./api";
import { clock } from "./format";
import { useLiveData } from "./live";
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

/** Compact age: "12s", "3m", "2h". */
function age(iso: string, now: number): string {
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86_400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86_400)}d`;
}

function status(agent: Agent): "busy" | "idle" | "draining" | "quiet" | "ended" {
  if (agent.state === "lost" || agent.state === "gone") return "ended";
  if (agent.state === "draining") return "draining";
  if (agent.quiet_since) return "quiet";
  return agent.claimed_tasks > 0 ? "busy" : "idle";
}

function shortName(name: string): string {
  // nanoclaw-socket-d6f8e4c0 → d6f8e4c0; otherwise keep a short label.
  const tail = name.split("-").pop();
  return tail && /^[a-f0-9]{6,}$/i.test(tail) ? tail.slice(0, 8) : name.slice(0, 14);
}

function engineLabel(agent: Agent): string {
  return agent.runtime?.engine ?? agent.runtime?.executor ?? "unknown";
}

/** Full-height rail of live workers; selection drives the main worker panel. */
export function WorkersSidebar({
  selectedId,
  onSelect,
}: {
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
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
          {listed.map((a) => {
            const kind = status(a);
            const eng = engineLabel(a);
            const selected = a.id === selectedId;
            return (
              <li key={a.id}>
                <button
                  type="button"
                  className={`worker-chip worker-${kind}${selected ? " selected" : ""}`}
                  title={[a.name, eng, a.end_reason].filter(Boolean).join(" · ")}
                  aria-current={selected ? "true" : undefined}
                  onClick={() => onSelect(a.id)}
                >
                  <span className={`worker-dot worker-state-${kind}`} />
                  <span className="worker-name">{shortName(a.name)}</span>
                  <span className="muted small worker-age">
                    {kind === "ended"
                      ? a.ended_at
                        ? age(a.ended_at, now)
                        : "—"
                      : age(a.last_heartbeat_at, now)}
                  </span>
                  <span className={`worker-state worker-state-${kind}`}>
                    {kind === "busy" ? "running" : kind}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
      <RailLog />
    </aside>
  );
}

function RailLog() {
  const { data } = useLiveData(
    (signal) => call(api.GET("/api/events", { params: { query: { limit: 8 } }, signal })),
    [],
  );
  const events: ArgusEvent[] = Array.isArray(data) ? data.slice(0, 8) : [];
  return (
    <div className="rail-log">
      <div className="rail-log-head">
        <p className="workers-rail-label">Live log</p>
        <a href="#setup/events">View all</a>
      </div>
      {data === undefined ? (
        <p className="muted small">…</p>
      ) : events.length === 0 ? (
        <p className="muted small">No events.</p>
      ) : (
        <ul>
          {events.map((event) => (
            <li key={event.id}>
              <time dateTime={event.at}>{clock(event.at)}</time>
              <span className={`badge ${logTone(event.kind)}`}>{logLabel(event.kind)}</span>
              <span>{kindText(event.kind)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function logTone(kind: string): string {
  if (/fail|error|lost|cancel/.test(kind)) return "state-failed";
  if (/warn|degrad|block|expir/.test(kind)) return "state-blocked";
  return "state-succeeded";
}

function logLabel(kind: string): string {
  if (/fail|error|lost|cancel/.test(kind)) return "error";
  if (/warn|degrad|block|expir/.test(kind)) return "warn";
  return "info";
}

/** `agent.lost` → "Agent lost". */
function kindText(kind: string): string {
  const text = kind.replaceAll(/[._]/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}
