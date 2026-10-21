import { type FormEvent, useState } from "react";
import { api, call, type Instance, type Pool } from "../api";
import { relative, shortId } from "../format";
import { useLiveData } from "../live";
import { envPatch } from "../pools";
import { LogTail } from "./AgentDetail";
import { NewWorker } from "./GuidedWorker";

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

export function PoolEditor({ pool, onClose }: { pool?: Pool; onClose: () => void }) {
  const [form, setForm] = useState({
    name: pool?.name ?? "",
    image: pool?.image ?? "",
    capabilities: pool?.capabilities.join(", ") ?? "",
    desired: pool?.desired_replicas ?? 0,
    max: pool?.max_replicas ?? 3,
    env: Object.entries(pool?.env ?? {})
      .map(([k, v]) => `${k}=${v}`)
      .join("\n"),
    extraHosts: pool?.docker.extra_hosts.join(", ") ?? "host.docker.internal:host-gateway",
    dockerHost: pool?.docker.host ?? "",
    target: (pool?.target ?? "docker") as "docker" | "kubernetes",
    k8sContext: pool?.kubernetes?.context ?? "",
    k8sNamespace: pool?.kubernetes?.namespace ?? "default",
  });
  const [error, setError] = useState<string>();
  const set = <K extends keyof typeof form>(key: K, value: (typeof form)[K]) =>
    setForm((f) => ({ ...f, [key]: value }));
  const list = (text: string) =>
    text
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError(undefined);
    const env = Object.fromEntries(
      form.env
        .split("\n")
        .map((line) => line.trim())
        .filter((line) => line.includes("="))
        .map((line) => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)]),
    );
    const docker = {
      ...(pool?.docker ?? {}),
      host: form.dockerHost || null,
      extra_hosts: list(form.extraHosts),
    };
    // Kubernetes placement is edited here too (a docker pool has none).
    const kubernetes =
      form.target === "kubernetes"
        ? {
            ...(pool?.kubernetes ?? {}),
            context: form.k8sContext,
            namespace: form.k8sNamespace,
          }
        : undefined;
    try {
      if (pool) {
        // A partial update guarded by the version we read: options the form does
        // not show are kept, and env values (write-only, shown as ***) are only
        // sent when changed.
        await call(
          api.PATCH("/api/pools/{name}", {
            params: { path: { name: pool.name } },
            body: {
              expected_version: pool.version,
              image: form.image,
              capabilities: list(form.capabilities),
              desired_replicas: form.desired,
              max_replicas: form.max,
              env: envPatch(pool.env, env),
              docker,
              ...(kubernetes && { kubernetes }),
            },
          }),
        );
      } else {
        await call(
          api.PUT("/api/pools/{name}", {
            params: { path: { name: form.name } },
            body: {
              target: form.target,
              ...(kubernetes && { kubernetes }),
              image: form.image,
              capabilities: list(form.capabilities),
              desired_replicas: form.desired,
              max_replicas: form.max,
              env,
              docker,
            },
          }),
        );
      }
      onClose();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div className="overlay">
      <form className="dialog" onSubmit={submit}>
        <h2>{pool ? `Edit pool ${pool.name}` : "New pool (advanced)"}</h2>
        <div className="row">
          <label>
            Name
            <input
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              pattern="[a-z0-9][a-z0-9\-]{0,39}"
              disabled={Boolean(pool)}
              required
            />
          </label>
          <label>
            Image
            <input value={form.image} onChange={(e) => set("image", e.target.value)} required />
          </label>
        </div>
        <div className="row">
          <label>
            Desired replicas
            <input
              type="number"
              min={0}
              value={form.desired}
              onChange={(e) => set("desired", Number(e.target.value))}
            />
          </label>
          <label>
            Max replicas (cap)
            <input
              type="number"
              min={0}
              value={form.max}
              onChange={(e) => set("max", Number(e.target.value))}
            />
          </label>
        </div>
        <label>
          Capabilities (comma-separated)
          <input value={form.capabilities} onChange={(e) => set("capabilities", e.target.value)} />
        </label>
        <label>
          Environment (KEY=value per line; *** keeps a stored value)
          <textarea rows={3} value={form.env} onChange={(e) => set("env", e.target.value)} />
        </label>
        {!pool && (
          <label>
            Target
            <select
              value={form.target}
              onChange={(e) => set("target", e.target.value as typeof form.target)}
            >
              <option value="docker">docker</option>
              <option value="kubernetes">kubernetes (one Pod per instance)</option>
            </select>
          </label>
        )}
        {form.target === "kubernetes" ? (
          <div className="row">
            <label>
              Kubeconfig context (or in-cluster)
              <input
                value={form.k8sContext}
                onChange={(e) => set("k8sContext", e.target.value)}
                required
              />
            </label>
            <label>
              Namespace
              <input
                value={form.k8sNamespace}
                onChange={(e) => set("k8sNamespace", e.target.value)}
                pattern="[a-z0-9]([\-a-z0-9]*[a-z0-9])?"
                required
              />
            </label>
          </div>
        ) : (
          <div className="row">
            <label>
              Docker host (empty: local)
              <input
                value={form.dockerHost}
                onChange={(e) => set("dockerHost", e.target.value)}
                placeholder="ssh://user@host"
              />
            </label>
            <label>
              Extra hosts
              <input value={form.extraHosts} onChange={(e) => set("extraHosts", e.target.value)} />
            </label>
          </div>
        )}
        {error && <p className="error">{error}</p>}
        <div className="actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="primary">
            Save
          </button>
        </div>
      </form>
    </div>
  );
}
