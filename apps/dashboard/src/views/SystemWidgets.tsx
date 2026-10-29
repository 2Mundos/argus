import type { SystemSummary } from "../api";
import { bytes } from "../format";

function duration(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 90) return `${Math.round(seconds)} s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

export function SystemWidgets({ data }: { data: SystemSummary }) {
  return (
    <section className="system">
      <div className="card">
        <h3>Background loops</h3>
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
            {Object.entries(data.loops).map(([name, loop]) => (
              <tr key={name}>
                <td>{name}</td>
                <td>
                  <span
                    className={`badge ${loop.status === "ok" ? "state-succeeded" : "state-failed"}`}
                  >
                    {loop.status}
                  </span>
                </td>
                <td>
                  {loop.last_success_age_s == null
                    ? "—"
                    : `${duration(loop.last_success_age_s)} ago`}
                </td>
                <td>
                  {loop.last_duration_s == null
                    ? "—"
                    : `${(loop.last_duration_s * 1000).toFixed(0)} ms`}
                </td>
                <td>{loop.passes_total}</td>
                <td>{loop.errors_total}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="muted small">
          Heartbeats and running state show that processes are alive, not that a model answers.
        </p>
      </div>

      <div className="system-facts">
        <div className="card">
          <h3>Right now</h3>
          <dl className="counts">
            <div>
              <dt>Oldest ready task</dt>
              <dd>{duration(data.oldest_ready_age_s)}</dd>
            </div>
            <div>
              <dt>Open decisions</dt>
              <dd>{data.decisions_open}</dd>
            </div>
            <div>
              <dt>Events written</dt>
              <dd>{data.events_total}</dd>
            </div>
          </dl>
        </div>
        <div className="card">
          <h3>Storage</h3>
          <dl className="counts">
            <div>
              <dt>Database</dt>
              <dd>{bytes(data.database.main_bytes)}</dd>
            </div>
            <div>
              <dt>WAL</dt>
              <dd>{bytes(data.database.wal_bytes)}</dd>
            </div>
            <div>
              <dt>Disk free</dt>
              <dd>{bytes(data.disk_free_bytes)}</dd>
            </div>
          </dl>
        </div>
      </div>

      <div className="card system-span">
        <h3>Pools</h3>
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
              {data.pools.map((pool) => (
                <tr key={pool.name}>
                  <td>{pool.name}</td>
                  <td>
                    <span className={`badge pool-${pool.status}`}>{pool.status}</span>
                  </td>
                  <td>
                    {pool.live} / {pool.desired}
                  </td>
                  <td>{pool.max}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
