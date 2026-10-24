import { useState } from "react";
import { api, call, type FleetHealth, type WorkspaceCleanupSummary } from "../api";
import { bytes, relative } from "../format";
import { useLiveData } from "../live";

export const NO_DATA = "no data";
const WINDOWS = [1, 6, 24, 168, 720];

export function seconds(value: number | null | undefined): string {
  if (value === null || value === undefined) return NO_DATA;
  if (value < 90) return `${Math.round(value)} s`;
  if (value < 5400) return `${(value / 60).toFixed(1)} min`;
  return `${(value / 3600).toFixed(1)} h`;
}

export function percent(value: number | null | undefined): string {
  return value === null || value === undefined ? NO_DATA : `${Math.round(value * 100)}%`;
}

export function tokens(value: number | null | undefined): string {
  if (value === null || value === undefined) return NO_DATA;
  return value >= 1000 ? `${(value / 1000).toFixed(1)}k` : String(Math.round(value));
}

function Caption({ window, source }: { window?: number; source: string }) {
  return (
    <span className="muted small">
      {window === undefined ? "current" : `${window} h`} · {source}
    </span>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="card">
      <h4>{title}</h4>
      {children}
    </div>
  );
}

export function Fleet() {
  const [window, setWindow] = useState(24);
  const { data, error, reload } = useLiveData(
    (signal) => call(api.GET("/api/fleet/health", { params: { query: { window } }, signal })),
    [window],
  );

  return (
    <section className="fleet">
      <div className="toolbar">
        <h2>Fleet</h2>
        <label>
          Window{" "}
          <select value={window} onChange={(e) => setWindow(Number(e.target.value))}>
            {WINDOWS.map((h) => (
              <option key={h} value={h}>
                {h} h
              </option>
            ))}
          </select>
        </label>
      </div>
      {error !== undefined && (
        <p className="error" role="alert">
          Fleet health failed to load: {error.message}{" "}
          <button type="button" onClick={reload}>
            Retry
          </button>
        </p>
      )}
      {data === undefined ? (
        <p className="muted">{error ? "Fleet health unavailable." : "Loading fleet health…"}</p>
      ) : (
        <Body h={data} />
      )}
      <WorkspaceCleanup />
    </section>
  );
}

function Body({ h }: { h: FleetHealth }) {
  const { queue_time: q, run_time: r, outcomes: o, failures: f, yield: y } = h;
  return (
    <>
      <h3>Pools</h3>
      <table>
        <thead>
          <tr>
            <th>Pool</th>
            <th>Status</th>
            <th>Live / desired</th>
            <th>Running</th>
            <th>Last heartbeat</th>
          </tr>
        </thead>
        <tbody>
          {h.pools.length === 0 && (
            <tr>
              <td colSpan={5}>{NO_DATA}</td>
            </tr>
          )}
          {h.pools.map((p) => (
            <tr key={p.name}>
              <td>{p.name}</td>
              <td>{p.status}</td>
              <td>
                {p.live} / {p.desired}
              </td>
              <td>{p.running}</td>
              <td>{p.last_heartbeat_at ? relative(p.last_heartbeat_at) : NO_DATA}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Caption source={h.pools[0]?.source ?? "pools"} />

      <h3>Tasks by state</h3>
      <div className="home-groups">
        {Object.entries(h.tasks_by_state.counts).map(([state, n]) => (
          <div className="stat" key={state}>
            <span className="n">{n}</span>
            <span className="muted small">{state}</span>
          </div>
        ))}
      </div>
      <Caption source={h.tasks_by_state.source} />

      <h3>Timing and outcomes</h3>
      <div className="home-groups">
        <Card title="Queue time">
          <p>
            p50 <strong>{seconds(q.p50)}</strong> · p90 <strong>{seconds(q.p90)}</strong>
          </p>
          <p className="muted small">
            mean {seconds(q.mean)} · n={q.n}
          </p>
          <Caption window={q.window_hours} source={q.source} />
        </Card>
        <Card title="Run time">
          <p>
            p50 <strong>{seconds(r.p50)}</strong> · p90 <strong>{seconds(r.p90)}</strong>
          </p>
          <p className="muted small">
            mean {seconds(r.mean)} · n={r.n}
          </p>
          <Caption window={r.window_hours} source={r.source} />
        </Card>
        <Card title="Outcomes">
          <p>
            success <strong>{percent(o.success_rate)}</strong> · failure{" "}
            <strong>{percent(o.failure_rate)}</strong>
          </p>
          <p className="muted small">
            {o.succeeded} succeeded · {o.failed} failed
          </p>
          <Caption window={o.window_hours} source={o.source} />
        </Card>
      </div>

      <h3>Yield</h3>
      <div className="home-groups">
        <Card title="Attempts">
          <p>
            <strong>{y.attempts}</strong> claimed · <strong>{y.completions}</strong> handed in a
            result
          </p>
          <p className="muted small">
            success per attempt {percent(y.attempt_success_rate)} · attempts per finished task{" "}
            {y.attempts_per_finished_task === null
              ? NO_DATA
              : y.attempts_per_finished_task.toFixed(1)}
          </p>
          <Caption window={y.window_hours} source={y.source} />
        </Card>
        <Card title="Tokens per task">
          <p>
            succeeded <strong>{tokens(y.tokens_per_succeeded)}</strong> · failed{" "}
            <strong>{tokens(y.tokens_per_failed)}</strong>
          </p>
          <p className="muted small">
            cost per succeeded{" "}
            {y.cost_per_succeeded === null ? NO_DATA : `$${y.cost_per_succeeded.toFixed(2)}`} ·
            usage reported for {y.usage_tasks} of {y.finished_tasks} finished tasks
          </p>
          <Caption window={y.window_hours} source={y.source} />
        </Card>
      </div>

      <h3>Failures by reason</h3>
      {f.groups.length === 0 ? (
        <p className="muted">No failures in the window.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Reason</th>
              <th>Class</th>
              <th>Count</th>
              <th>Time held</th>
              <th>Recent examples</th>
            </tr>
          </thead>
          <tbody>
            {f.groups.map((g) => (
              <tr key={g.reason}>
                <td>{g.reason}</td>
                <td>{g.retry_class}</td>
                <td>{g.count}</td>
                <td>{seconds(g.lost_seconds)}</td>
                <td>
                  <ul className="plain">
                    {g.examples.map((e) => (
                      <li key={`${e.task_id}-${e.at}`}>
                        <a href={`#tasks/${e.task_id}`}>{e.title}</a>{" "}
                        <span className="muted small">{relative(e.at)}</span>
                        <div className="small">{e.error || NO_DATA}</div>
                      </li>
                    ))}
                  </ul>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <Caption window={f.window_hours} source={f.source} />
      {(f.truncated || h.truncated) && (
        <p className="muted small">Showing the newest 5000 rows only.</p>
      )}
    </>
  );
}

/** Optional: a token without fleet-manager access gets no card, not an error. */
function WorkspaceCleanup() {
  const { data, reload } = useLiveData(
    (signal) =>
      call(api.GET("/api/fleet/workspace-cleanup", { signal })).catch(
        (): WorkspaceCleanupSummary | null => null,
      ),
    [],
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  if (!data) return null;
  const { settings: s, last_run: run } = data;

  async function dryRun() {
    setBusy(true);
    setError(undefined);
    try {
      await call(api.POST("/api/fleet/workspace-cleanup/run", { body: { dry_run: true } }));
      reload();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h3>Maintenance</h3>
      <div className="home-groups">
        <Card title="Workspace cleanup">
          <p>
            <strong>{s.enabled ? "enabled" : "disabled"}</strong> ·{" "}
            {s.dry_run ? "dry-run mode" : "deletes archived workspaces"}
            {data.running && " · running"}
          </p>
          <p className="muted small">archive root {s.archive_root ?? "not set"}</p>
          <p className="small">
            last run{" "}
            {run
              ? `${relative(run.started_at)} · ${run.dry_run ? "dry" : "real"} · ${run.status}`
              : NO_DATA}
          </p>
          <p className="small">
            {Object.entries(data.counts)
              .map(([status, n]) => `${n} ${status}`)
              .join(" · ") || NO_DATA}
          </p>
          <p className="muted small">
            approx. {bytes(data.bytes_estimated)} estimated · {bytes(data.bytes_freed)} freed
          </p>
          <button type="button" disabled={busy} onClick={() => void dryRun()}>
            Dry run now
          </button>
          {error && <p className="error">Dry run failed: {error}</p>}
        </Card>
      </div>
    </>
  );
}
