import { type Agent, api, call } from "./api";
import { shortId } from "./format";
import { useLiveData } from "./live";
import { useNow } from "./views/Activity";

function liveWorkers(agents: Agent[] | undefined): Agent[] {
  return (agents ?? []).filter(
    (a) => a.role === "worker" && (a.state === "active" || a.state === "draining"),
  );
}

/** Compact age: "12s", "3m", "2h". */
function age(iso: string, now: number): string {
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  if (s < 86_400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86_400)}d`;
}

function status(agent: Agent): "busy" | "idle" | "draining" {
  if (agent.state === "draining") return "draining";
  return agent.claimed_tasks > 0 ? "busy" : "idle";
}

function shortName(name: string): string {
  // nanoclaw-socket-d6f8e4c0 → d6f8e4c0; otherwise keep a short label.
  const tail = name.split("-").pop();
  return tail && /^[a-f0-9]{6,}$/i.test(tail) ? tail.slice(0, 8) : name.slice(0, 14);
}

type EngineKind = "claude" | "codex" | "cursor" | "other";

function engineKind(agent: Agent): EngineKind {
  const raw = `${agent.runtime?.engine ?? ""} ${agent.runtime?.executor ?? ""}`.toLowerCase();
  if (/\b(claude|anthropic)\b/.test(raw)) return "claude";
  if (/\b(codex|openai)\b/.test(raw)) return "codex";
  if (/\b(cursor|agent)\b/.test(raw)) return "cursor";
  return "other";
}

function engineLabel(agent: Agent): string {
  return agent.runtime?.engine ?? agent.runtime?.executor ?? "unknown";
}

/** Engine glyph for the rail; falls back to a letter. */
function WorkerMark({ agent }: { agent: Agent }) {
  const kind = engineKind(agent);
  const label = engineLabel(agent);
  if (kind === "other") {
    return (
      <span className="worker-mark letter" aria-hidden="true">
        {label.charAt(0).toUpperCase() || "?"}
      </span>
    );
  }
  const path =
    kind === "claude"
      ? "M13.6 4h3.1L22 20h-3.1ZM7.3 4h3.2L15.8 20h-3.2l-1.1-3.4H6.1L5 20H1.9Zm-.3 9.9h3.5L8.8 8.4Z"
      : kind === "codex"
        ? "M12 2.5a4.5 4.5 0 0 1 4.3 3.1 4.5 4.5 0 0 1 4.4 6.8 4.5 4.5 0 0 1-3 7.1 4.5 4.5 0 0 1-7.4 1.9 4.5 4.5 0 0 1-6.9-3.4 4.5 4.5 0 0 1-1.5-7.6A4.5 4.5 0 0 1 8 3.6a4.5 4.5 0 0 1 4-1.1Zm0 5.5-3.5 2v4L12 16l3.5-2v-4Z"
        : "M12 2 21 7v10l-9 5-9-5V7Zm0 2.3L5.2 8 12 11.8 18.8 8Zm-7 5.4v6.2l6 3.4v-6.3Zm14 0-6 3.3v6.3l6-3.4Z";
  return (
    <svg className={`worker-mark engine-${kind}`} viewBox="0 0 24 24" aria-hidden="true">
      <path d={path} fill="currentColor" fillRule="evenodd" />
    </svg>
  );
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
  const busy = workers.filter((a) => a.claimed_tasks > 0).length;

  return (
    <aside className="workers-rail" aria-label="Running workers">
      <header className="workers-rail-head">
        <p className="workers-rail-label">Workers</p>
        {agents !== undefined && (
          <p className="workers-rail-summary muted small">
            {workers.length} live
            {busy > 0 ? ` · ${busy} busy` : ""}
          </p>
        )}
      </header>
      {agents === undefined ? (
        <p className="workers-rail-empty muted small">{error ? "—" : "…"}</p>
      ) : workers.length === 0 ? (
        <p className="workers-rail-empty muted small">None live</p>
      ) : (
        <ul>
          {workers.map((a) => {
            const kind = status(a);
            const eng = engineLabel(a);
            const exec = a.runtime?.executor;
            const caps = a.capabilities;
            const selected = a.id === selectedId;
            return (
              <li key={a.id}>
                <button
                  type="button"
                  className={`worker-chip worker-${kind}${selected ? " selected" : ""}`}
                  title={a.name}
                  aria-current={selected ? "true" : undefined}
                  onClick={() => onSelect(a.id)}
                >
                  <span className="worker-chip-top">
                    <WorkerMark agent={a} />
                    <span className={`worker-state worker-state-${kind}`}>{kind}</span>
                  </span>
                  <span className="worker-name">{shortName(a.name)}</span>
                  <span className="worker-stats">
                    <span>
                      <span className="muted">tasks</span> {a.claimed_tasks}
                    </span>
                    <span>
                      <span className="muted">hb</span> {age(a.last_heartbeat_at, now)}
                    </span>
                    <span>
                      <span className="muted">up</span> {age(a.created_at, now)}
                    </span>
                    <span className="worker-engine" title={[eng, exec].filter(Boolean).join(" · ")}>
                      {eng}
                      {exec && exec !== eng ? ` · ${exec}` : ""}
                    </span>
                    {caps.length > 0 && (
                      <span className="worker-caps muted" title={caps.join(", ")}>
                        {caps.length} cap{caps.length === 1 ? "" : "s"}
                        {a.instance_id ? ` · ${shortId(a.instance_id)}` : ""}
                      </span>
                    )}
                    {caps.length === 0 && a.instance_id && (
                      <span className="worker-caps muted">{shortId(a.instance_id)}</span>
                    )}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </aside>
  );
}
