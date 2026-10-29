import { useState } from "react";
import { api, call, type Instance, type Pool } from "../api";
import { relative, shortId } from "../format";
import { useLiveData } from "../live";
import { LogTail } from "./AgentDetail";
import { NewWorker } from "./GuidedWorker";
import { PoolEditor } from "./PoolEditor";

export { PoolEditor };

export function Pools() {
  const [editing, setEditing] = useState<Pool | "new" | null>(null);
  const [guided, setGuided] = useState(false);
  const { data: pools, error } = useLiveData(
    (signal) => call(api.GET("/api/pools", { signal })),
    [],
  );
  return (
    <section>
      <div className="toolbar">
        <h2>Pools</h2>
        <button type="button" className="primary" onClick={() => setGuided((g) => !g)}>
          New worker
        </button>
        <button type="button" onClick={() => setEditing("new")}>
          New pool (advanced)
        </button>
      </div>
      <p className="page-lead">Desired and running workers, and the last error from each pool.</p>
      {error && <p className="error">{error.message}</p>}
      {(guided || pools?.length === 0) && (
        <NewWorker onCancel={guided ? () => setGuided(false) : undefined} />
      )}
      {(pools ?? []).map((pool) => (
        <PoolCard key={pool.name} pool={pool} onEdit={() => setEditing(pool)} />
      ))}
      {editing && (
        <PoolEditor
          pool={editing === "new" ? undefined : editing}
          onClose={() => setEditing(null)}
        />
      )}
    </section>
  );
}

function PoolCard({ pool, onEdit }: { pool: Pool; onEdit: () => void }) {
  const [error, setError] = useState<string>();
  const [logsOf, setLogsOf] = useState<Instance | null>(null);
  const { data: instances } = useLiveData(
    (signal) => call(api.GET("/api/instances", { params: { query: { pool: pool.name } }, signal })),
    [pool.name],
  );

  async function scale(desired: number) {
    setError(undefined);
    try {
      await call(
        api.POST("/api/pools/{name}/scale", {
          params: { path: { name: pool.name } },
          body: { desired_replicas: Math.max(0, desired) },
        }),
      );
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function terminate(instance: Instance) {
    setError(undefined);
    try {
      await call(
        api.POST("/api/instances/{instance_id}/terminate", {
          params: { path: { instance_id: instance.id } },
        }),
      );
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <article className="card pool">
      <header className="toolbar">
        <h3>
          {pool.name} <span className={`badge pool-${pool.status}`}>{pool.status}</span>
        </h3>
        <span className="muted small">
          {pool.image} · {pool.target}/{pool.runtime}
        </span>
        <span className="scale">
          <button type="button" onClick={() => scale(pool.desired_replicas - 1)}>
            −
          </button>
          <strong>
            {pool.live_instances} / {pool.desired_replicas}
          </strong>
          <button
            type="button"
            onClick={() => scale(pool.desired_replicas + 1)}
            disabled={pool.desired_replicas >= pool.max_replicas}
          >
            +
          </button>
          <span className="muted small">max {pool.max_replicas}</span>
        </span>
        <button type="button" className="link" onClick={onEdit}>
          Edit
        </button>
      </header>
      {pool.last_error && <p className="error small">{pool.last_error}</p>}
      {error && <p className="error">{error}</p>}
      <table>
        <thead>
          <tr>
            <th>Instance</th>
            <th>State</th>
            <th>Agents</th>
            <th>Since</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(instances ?? []).map((i) => (
            <tr key={i.id}>
              <td title={i.provider_ref}>{shortId(i.id)}</td>
              <td>
                <span className={`badge instance-${i.state}`}>{i.state}</span>
                {i.draining && <span className="muted small"> draining</span>}
                {i.exit_reason && <span className="muted small"> {i.exit_reason}</span>}
                {i.last_error && i.state === "reserved" && (
                  <span className="error small"> {i.last_error}</span>
                )}
              </td>
              <td className="small">
                {i.agents.length === 0
                  ? "—"
                  : i.agents
                      .map((a) => `${a.name} (${a.state}${a.claimed_tasks ? ", busy" : ""})`)
                      .join(", ")}
              </td>
              <td className="small">{relative(i.running_at ?? i.created_at)}</td>
              <td className="row-actions">
                <button type="button" className="link" onClick={() => setLogsOf(i)}>
                  Logs
                </button>
                {(i.state === "running" || i.state === "reserved") && (
                  <button type="button" className="link danger" onClick={() => terminate(i)}>
                    Terminate
                  </button>
                )}
              </td>
            </tr>
          ))}
          {instances?.length === 0 && (
            <tr>
              <td colSpan={5} className="muted">
                No instances.
              </td>
            </tr>
          )}
        </tbody>
      </table>
      {logsOf && <Logs instance={logsOf} onClose={() => setLogsOf(null)} />}
    </article>
  );
}

function Logs({ instance, onClose }: { instance: Instance; onClose: () => void }) {
  return (
    <div className="overlay">
      <div className="dialog wide">
        <div className="toolbar">
          <h2>Logs · {shortId(instance.id)}</h2>
          <button type="button" className="link" onClick={onClose}>
            Close
          </button>
        </div>
        <LogTail instanceId={instance.id} tail={500} />
      </div>
    </div>
  );
}
