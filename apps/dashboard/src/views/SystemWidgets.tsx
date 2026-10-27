import type { SystemSummary } from "../api";
import { bytes } from "../format";

function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

export function SystemWidgets({ data }: { data: SystemSummary }) {
  const ready = data.readiness.status === "ready";
  return (
    <section className="system">
      <div className="toolbar">
        <h3>System</h3>
        <span className={`badge ${ready ? "state-succeeded" : "state-failed"}`}>
          {ready ? "ready" : "not ready"}
        </span>
        <span className="muted small">
          v{data.version} · up {duration(data.uptime_s)}
        </span>
      </div>

      <h4>Checks</h4>
      <table>
        <tbody>
          {Object.entries(data.readiness.checks).map(([name, check]) => (
            <tr key={name}>
              <td>{name}</td>
              <td>
                <span
                  className={`badge ${
                    check.status === "ok"
                      ? "state-succeeded"
                      : check.status === "degraded"
                        ? "state-blocked"
                        : "state-failed"
                  }`}
                >
                  {check.status}
                </span>
              </td>
              <td className="muted small">{check.detail ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h4>Background loops</h4>
      <table>
        <thead>
          <tr>
            <th>Loop</th>
            <th>Status</th>
            <th>Last pass</th>
            <th>Took</th>
            <th>Passes</th>
            <th>Errors</th>
          </tr>
        </thead>
        <tbody>
          {Object.entries(data.loops).map(([name, l]) => (
            <tr key={name}>
              <td>{name}</td>
              <td>
                <span className={`badge ${l.status === "ok" ? "state-succeeded" : "state-failed"}`}>
                  {l.status}
                </span>
              </td>
              <td>
                {l.last_success_age_s == null ? "—" : `${duration(l.last_success_age_s)} ago`}
              </td>
              <td>
                {l.last_duration_s == null ? "—" : `${(l.last_duration_s * 1000).toFixed(0)} ms`}
              </td>
              <td>{l.passes_total}</td>
              <td>{l.errors_total}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <h4>Work</h4>
      <div className="stats">
        {Object.entries(data.tasks).map(([state, n]) => (
          <div className="stat" key={state}>
            <span className="n">{n}</span>
            <span className="muted small">{state.replace("_", " ")}</span>
          </div>
        ))}
      </div>
      <p className="small">
        Oldest ready task waiting: <strong>{duration(data.oldest_ready_age_s)}</strong> · open
        decisions: <strong>{data.decisions_open}</strong> · events written:{" "}
        <strong>{data.events_total}</strong>
      </p>

      <h4>Agents</h4>
      <div className="stats">
        {Object.entries(data.agents).map(([state, n]) => (
          <div className="stat" key={state}>
            <span className="n">{n}</span>
            <span className="muted small">{state}</span>
          </div>
        ))}
      </div>

      <h4>Pools</h4>
      {data.pools.length === 0 ? (
        <p className="muted small">No pools.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Pool</th>
              <th>Status</th>
              <th>Live / desired</th>
              <th>Cap</th>
            </tr>
          </thead>
          <tbody>
            {data.pools.map((p) => (
              <tr key={p.name}>
                <td>{p.name}</td>
                <td>
                  <span className={`badge pool-${p.status}`}>{p.status}</span>
                </td>
                <td>
                  {p.live} / {p.desired}
                </td>
                <td>{p.max}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h4>Storage</h4>
      <p className="small">
        Database {bytes(data.database.main_bytes)} (WAL {bytes(data.database.wal_bytes)}) · disk
        free {bytes(data.disk_free_bytes)}
      </p>
    </section>
  );
}
