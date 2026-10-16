import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, call, type FleetCapabilities } from "../api";
import { relative, shortId } from "../format";
import { launchGate, launchStatus } from "../launch";
import { useLiveData } from "../live";
import { NewWorker } from "./GuidedWorker";

function CreateWorker({ onError }: { onError: (message?: string) => void }) {
  const { data: pools } = useLiveData(() => call(api.GET("/api/pools")), []);
  const { data: instances } = useLiveData(() => call(api.GET("/api/instances")), []);
  const [caps, setCaps] = useState<FleetCapabilities>();
  const [capsError, setCapsError] = useState<string>();
  const [chosen, setChosen] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [requested, setRequested] = useState<string>();
  const [showNew, setShowNew] = useState(false);

  const check = useCallback(async () => {
    setCapsError(undefined);
    try {
      setCaps(await call(api.GET("/api/fleet/capabilities")));
    } catch (err) {
      setCapsError((err as Error).message);
    }
  }, []);
  useEffect(() => {
    void check();
  }, [check]);

  const pool = (pools ?? []).find((p) => p.name === chosen) ?? pools?.[0];
  const capability = caps?.pools.find((c) => c.name === pool?.name);
  const gate = launchGate(pool, capability, busy);
  const mine = useMemo(
    () => (pool ? (instances ?? []).filter((i) => i.pool === pool.name) : []),
    [pool, instances],
  );

  const inFlight = useRef(false); // a double click submits once, even before a re-render

  async function launch() {
    if (!pool || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    onError(undefined);
    setRequested(undefined);
    try {
      const res = await call(
        api.POST("/api/pools/{name}/launch", {
          params: { path: { name: pool.name } },
          body: { expected_desired_replicas: pool.desired_replicas },
        }),
      );
      setRequested(`Requested a worker from ${res.pool.name} (wants ${res.desired_replicas}).`);
    } catch (err) {
      onError((err as Error).message);
    } finally {
      inFlight.current = false;
      setBusy(false);
      void check();
    }
  }

  // A fresh install has no pools: the first worker comes from a template.
  if (pools?.length === 0) return <NewWorker />;

  return (
    <div className="card">
      <div className="toolbar">
        <h3>Create worker</h3>
        <select
          aria-label="Pool"
          value={pool?.name ?? ""}
          onChange={(e) => setChosen(e.target.value)}
          disabled={!pools?.length}
        >
          {(pools ?? []).map((p) => (
            <option key={p.name} value={p.name}>
              {p.name} ({p.runtime}/{p.target})
            </option>
          ))}
        </select>
        <button type="button" className="primary" disabled={!gate.enabled} onClick={launch}>
          {busy ? "Requesting…" : "Create worker"}
        </button>
        <button type="button" className="link" onClick={() => void check()}>
          Re-check node
        </button>
        <button type="button" onClick={() => setShowNew((v) => !v)}>
          {showNew ? "Hide new worker" : "New worker from template…"}
        </button>
      </div>
      {showNew && <NewWorker onCancel={() => setShowNew(false)} />}
      {capsError && <p className="error">Could not read node capabilities: {capsError}</p>}
      {gate.reason && <p className="muted small">{gate.reason}</p>}
      {caps && !caps.public_url_configured && (
        <p className="error small">ARGUS_PUBLIC_URL is not set: no worker can reach Argus.</p>
      )}
      {requested && <p className="small">{requested}</p>}
      {pool && (
        <p className="small">
          {pool.name}: {launchStatus(pool, mine)} · {pool.live_instances} of {pool.max_replicas}{" "}
          slots in use
        </p>
      )}
      {capability?.checks
        .filter((c) => c.ok !== true)
        .map((c) => (
          <p key={c.name} className="muted small">
            {c.ok === null ? "unverified" : "blocked"} · {c.detail}
          </p>
        ))}
      {mine.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>Instance</th>
              <th>State</th>
              <th>Attempts</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {mine.map((i) => (
              <tr key={i.id}>
                <td title={i.id}>{shortId(i.id)}</td>
                <td>
                  <span className="badge">{i.state}</span>
                </td>
                <td>{i.start_attempts}</td>
                <td className="small">
                  {i.last_error ?? i.exit_reason ?? i.agents.map((a) => a.name).join(", ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function Agents() {
  const [showEnded, setShowEnded] = useState(false);
  const [issued, setIssued] = useState<{ token: string; expires_at: string }>();
  const [error, setError] = useState<string>();
  const { data: agents } = useLiveData(() => call(api.GET("/api/agents")), []);
  const visible = (agents ?? []).filter(
    (a) => showEnded || a.state === "active" || a.state === "draining",
  );

  async function mint(role: "worker" | "orchestrator") {
    setError(undefined);
    try {
      setIssued(await call(api.POST("/api/agents/bootstrap-tokens", { body: { role } })));
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function drain(agentId: string) {
    try {
      await call(
        api.POST("/api/agents/{agent_id}/drain", { params: { path: { agent_id: agentId } } }),
      );
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <section>
      <div className="toolbar">
        <h2>Agents</h2>
        <label className="check">
          <input
            type="checkbox"
            checked={showEnded}
            onChange={(e) => setShowEnded(e.target.checked)}
          />
          Show ended
        </label>
        <button type="button" onClick={() => mint("worker")}>
          Worker bootstrap token
        </button>
        <button type="button" onClick={() => mint("orchestrator")}>
          Orchestrator bootstrap token
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      <CreateWorker onError={setError} />
      {issued && (
        <div className="card notice">
          <p>
            One-time bootstrap token (shown once, expires {relative(issued.expires_at)}). An agent
            redeems it at <code>POST /api/agents/register</code>.
          </p>
          <code className="secret">{issued.token}</code>
          <div className="actions left">
            <button type="button" onClick={() => navigator.clipboard.writeText(issued.token)}>
              Copy
            </button>
            <button type="button" className="link" onClick={() => setIssued(undefined)}>
              Dismiss
            </button>
          </div>
        </div>
      )}
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Role</th>
            <th>State</th>
            <th>Tasks</th>
            <th>Heartbeat</th>
            <th>Capabilities</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {visible.map((a) => (
            <tr key={a.id}>
              <td title={a.id}>
                {a.name} <span className="muted small">{shortId(a.id)}</span>
              </td>
              <td>{a.role}</td>
              <td>
                <span className={`badge agent-${a.state}`}>
                  {a.state === "active" ? (a.claimed_tasks > 0 ? "busy" : "idle") : a.state}
                </span>
                {a.end_reason && <span className="muted small"> {a.end_reason}</span>}
              </td>
              <td>{a.claimed_tasks}</td>
              <td>{relative(a.last_heartbeat_at)}</td>
              <td className="small">{a.capabilities.join(", ") || "—"}</td>
              <td>
                {a.state === "active" && (
                  <button type="button" className="link" onClick={() => drain(a.id)}>
                    Drain
                  </button>
                )}
              </td>
            </tr>
          ))}
          {visible.length === 0 && (
            <tr>
              <td colSpan={7} className="muted">
                {agents && agents.length > 0
                  ? `No live agents (${agents.length} ended; tick "Show ended" to see them).`
                  : "No agents yet. Mint a bootstrap token and start one."}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </section>
  );
}
