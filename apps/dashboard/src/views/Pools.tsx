import { type FormEvent, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { api, call, type Instance, type Pool } from "../api";
import { DateChip } from "../DateChip";
import { shortId } from "../format";
import { useLiveData } from "../live";
import { PageHeader } from "../PageHeader";
import { LogTail } from "./AgentDetail";
import { NewWorker } from "./GuidedWorker";
import { PoolEditor } from "./PoolEditor";

export { PoolEditor };

export function Pools() {
  const [editing, setEditing] = useState<Pool | "new" | null>(null);
  const [deleting, setDeleting] = useState<Pool | null>(null);
  const [notice, setNotice] = useState<string>();
  const [guided, setGuided] = useState(false);
  const {
    data: pools,
    error,
    reload,
  } = useLiveData((signal) => call(api.GET("/api/pools", { signal })), []);
  return (
    <section>
      <PageHeader
        title="Pools"
        lead="Desired and running workers, and the last error from each pool."
      >
        <button type="button" className="primary" onClick={() => setGuided((g) => !g)}>
          New worker
        </button>
        <button type="button" onClick={() => setEditing("new")}>
          New pool (advanced)
        </button>
      </PageHeader>
      {notice && <p className="notice">{notice}</p>}
      {error && <p className="error">{error.message}</p>}
      {(guided || pools?.length === 0) && (
        <NewWorker onCancel={guided ? () => setGuided(false) : undefined} />
      )}
      {(pools ?? []).map((pool) => (
        <PoolCard
          key={pool.name}
          pool={pool}
          onEdit={() => setEditing(pool)}
          onDelete={pool.system ? undefined : () => setDeleting(pool)}
        />
      ))}
      {editing && (
        <PoolEditor
          pool={editing === "new" ? undefined : editing}
          onClose={() => setEditing(null)}
        />
      )}
      {deleting && (
        <DeletePool
          pool={deleting}
          onClose={() => setDeleting(null)}
          onDone={(message) => {
            setDeleting(null);
            setNotice(message);
            reload();
          }}
        />
      )}
    </section>
  );
}

function PoolCard({
  pool,
  onEdit,
  onDelete,
}: {
  pool: Pool;
  onEdit: () => void;
  onDelete?: () => void;
}) {
  const [error, setError] = useState<string>();
  const [logsOf, setLogsOf] = useState<Instance | null>(null);
  const action = useAction();
  const { data: instances } = useLiveData(
    (signal) => call(api.GET("/api/instances", { params: { query: { pool: pool.name } }, signal })),
    [pool.name],
  );

  async function scale(desired: number, key: string) {
    setError(undefined);
    await action.run(async () => {
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
    }, key);
  }

  async function terminate(instance: Instance) {
    setError(undefined);
    await action.run(async () => {
      try {
        await call(
          api.POST("/api/instances/{instance_id}/terminate", {
            params: { path: { instance_id: instance.id } },
          }),
        );
      } catch (err) {
        setError((err as Error).message);
      }
    }, `terminate:${instance.id}`);
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
          <ActionButton
            pending={action.is("scale-down")}
            disabled={action.busy}
            action={() => scale(pool.desired_replicas - 1, "scale-down")}
          >
            −
          </ActionButton>
          <strong>
            {pool.live_instances} / {pool.desired_replicas}
          </strong>
          <ActionButton
            pending={action.is("scale-up")}
            disabled={action.busy || pool.desired_replicas >= pool.max_replicas}
            action={() => scale(pool.desired_replicas + 1, "scale-up")}
          >
            +
          </ActionButton>
          <span className="muted small">max {pool.max_replicas}</span>
        </span>
        <button type="button" className="link" onClick={onEdit}>
          Edit
        </button>
        {onDelete && (
          <button type="button" className="link danger" onClick={onDelete}>
            Delete
          </button>
        )}
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
              <td className="small">
                <DateChip at={i.running_at ?? i.created_at} />
              </td>
              <td className="row-actions">
                <button type="button" className="link" onClick={() => setLogsOf(i)}>
                  Logs
                </button>
                {(i.state === "running" || i.state === "reserved") && (
                  <ActionButton
                    className="link danger"
                    pending={action.is(`terminate:${i.id}`)}
                    disabled={action.busy}
                    action={() => terminate(i)}
                  >
                    Terminate
                  </ActionButton>
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

/** Deleting needs the pool's name typed back. The server checks it again against
 * the current name, so a stale dialog cannot delete a renamed target. */
export function DeletePool({
  pool,
  onClose,
  onDone,
}: {
  pool: Pool;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string>();
  const action = useAction();

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (typed !== pool.name) return;
    setError(undefined);
    await action.run(async () => {
      try {
        await call(
          api.DELETE("/api/pools/{name}", {
            params: { path: { name: pool.name }, query: { confirm_name: typed } },
          }),
        );
        onDone(`Deleted “${pool.name}”.`);
      } catch (err) {
        setError((err as Error).message);
      }
    });
  }

  return (
    <div className="overlay">
      <form className="dialog" onSubmit={submit} aria-label={`Delete ${pool.name}`}>
        <h2>Delete {pool.name}?</h2>
        <p className="small">
          This removes the pool definition and its terminated instance history. It cannot be undone.
          Scale to zero and wait until every instance is terminated first; delete never stops
          containers itself.
        </p>
        <label>
          Type <strong>{pool.name}</strong> to confirm
          <input
            aria-label="Confirm pool name"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            autoComplete="off"
            spellCheck={false}
          />
        </label>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="actions">
          <button type="button" disabled={action.busy} onClick={onClose}>
            Cancel
          </button>
          <ActionButton
            type="submit"
            className="danger"
            disabled={typed !== pool.name}
            pending={action.busy}
          >
            Delete pool
          </ActionButton>
        </div>
      </form>
    </div>
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
