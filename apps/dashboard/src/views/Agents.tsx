import { useState } from "react";
import { api, call } from "../api";
import { relative, shortId } from "../format";
import { useLiveData } from "../live";

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
