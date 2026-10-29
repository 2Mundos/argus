import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { type AgentHygiene, api, call, type FleetCapabilities } from "../api";
import { relative, shortId } from "../format";
import { launchGate, launchStatus } from "../launch";
import { useLiveData } from "../live";
import { AgentDetail } from "./AgentDetail";
import { NewWorker } from "./GuidedWorker";

const AGE_BUCKETS = [
  ["under_1d", "Under 1 day"],
  ["1d_to_7d", "1–7 days"],
  ["7d_to_30d", "7–30 days"],
  ["30d_and_over", "30 days and over"],
] as const;

function CreateWorker({ onError }: { onError: (message?: string) => void }) {
  const { data: pools } = useLiveData((signal) => call(api.GET("/api/pools", { signal })), []);
  const { data: instances } = useLiveData(
    (signal) => call(api.GET("/api/instances", { signal })),
    [],
  );
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
        <p className="error small">
          ARGUS_PUBLIC_URL is not set: no worker can reach Argus. Set it to the address workers use
          to reach this Argus (README, Run with Docker).
        </p>
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
  const [opened, setOpened] = useState<string | null>(null);
  const [issued, setIssued] = useState<{ token: string; expires_at: string }>();
  const [error, setError] = useState<string>();
  const [archiveOpen, setArchiveOpen] = useState(false);
  const { data: agents } = useLiveData((signal) => call(api.GET("/api/agents", { signal })), []);
  const {
    data: hygiene,
    error: hygieneError,
    reload: reloadHygiene,
  } = useLiveData((signal) => call(api.GET("/api/agents/hygiene", { signal })), []);
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

  if (opened) {
    return <AgentDetail agentId={opened} onClose={() => setOpened(null)} variant="panel" />;
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
        <button type="button" onClick={() => setArchiveOpen(true)}>
          Archive old identities…
        </button>
      </div>
      <p className="page-lead">Registered agents, their role and the last heartbeat.</p>
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
      <HygieneSection hygiene={hygiene} error={hygieneError?.message} onRetry={reloadHygiene} />
      {archiveOpen && (
        <ArchiveAgents
          onClose={() => {
            setArchiveOpen(false);
            reloadHygiene();
          }}
        />
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
                <button type="button" className="link" onClick={() => setOpened(a.id)}>
                  {a.name}
                </button>{" "}
                <span className="muted small">{shortId(a.id)}</span>
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

function HygieneSection(props: {
  hygiene: AgentHygiene | undefined;
  error: string | undefined;
  onRetry: () => void;
}) {
  const { hygiene, error, onRetry } = props;
  if (hygiene === undefined) {
    return (
      <div className="card">
        <h3>Lost and gone</h3>
        {error !== undefined ? (
          <p className="error" role="alert">
            Lost and gone failed to load: {error}{" "}
            <button type="button" onClick={onRetry}>
              Retry
            </button>
          </p>
        ) : (
          <p className="muted">Loading lost and gone…</p>
        )}
      </div>
    );
  }
  const t = hygiene.totals;
  const spike = hygiene.lost_spike;
  return (
    <div className="card">
      <h3>Lost and gone</h3>
      <p className="small">
        {t.lost} lost · {t.gone} gone · {t.archived} archived
        {t.lost_holding_claims > 0 &&
          ` · ${t.lost_holding_claims} lost still holding a claimed task`}
      </p>
      {spike.triggered && (
        <p className="error small" role="status">
          {spike.current} agents lost in the last {spike.window_hours}h, up from {spike.previous} in
          the window before.
        </p>
      )}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>State</th>
              {AGE_BUCKETS.map(([key, label]) => (
                <th key={key}>{label}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(["lost", "gone"] as const).map((state) => (
              <tr key={state}>
                <td>{state}</td>
                {AGE_BUCKETS.map(([key]) => (
                  <td key={key}>{hygiene.age_buckets[state]?.[key] ?? 0}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Pool</th>
              <th>State</th>
              <th>Day</th>
              <th>Count</th>
              <th>Max age (h)</th>
            </tr>
          </thead>
          <tbody>
            {hygiene.groups.map((g) => (
              <tr key={`${g.pool}-${g.state}-${g.day}`}>
                <td>{g.pool}</td>
                <td>{g.state}</td>
                <td>{g.day}</td>
                <td>{g.count}</td>
                <td>{g.max_age_hours.toFixed(1)}</td>
              </tr>
            ))}
            {hygiene.groups.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  No lost or gone identities.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {hygiene.claim_holders.length > 0 && (
        <>
          <h4>Lost agents holding a claimed task</h4>
          <ul className="small">
            {hygiene.claim_holders.map((h) => (
              <li key={`${h.agent_id}-${h.task_id}`}>
                agent {shortId(h.agent_id)} · task {shortId(h.task_id)}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function ArchiveAgents({ onClose }: { onClose: () => void }) {
  const [states, setStates] = useState<Array<"lost" | "gone">>(["lost", "gone"]);
  const [olderThanDays, setOlderThanDays] = useState("30");
  const [count, setCount] = useState<number>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setCount(undefined);
    if (states.length === 0) {
      setCount(0);
      return;
    }
    const days = olderThanDays.trim() === "" ? undefined : Number(olderThanDays);
    if (days !== undefined && (!Number.isInteger(days) || days < 0)) {
      setCount(0);
      return;
    }
    let cancelled = false;
    call(
      api.POST("/api/agents/archive", {
        body: {
          states,
          ...(days !== undefined ? { older_than_days: days } : {}),
          dry_run: true,
        },
      }),
    ).then(
      (result) => {
        if (!cancelled) setCount(result.count);
      },
      (err: Error) => {
        if (!cancelled) setError(err.message);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [states, olderThanDays]);

  async function confirm() {
    if (states.length === 0) return;
    const days = olderThanDays.trim() === "" ? undefined : Number(olderThanDays);
    setBusy(true);
    setError(undefined);
    try {
      await call(
        api.POST("/api/agents/archive", {
          body: {
            states,
            ...(days !== undefined ? { older_than_days: days } : {}),
            dry_run: false,
          },
        }),
      );
      onClose();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  const toggle = (state: "lost" | "gone", on: boolean) =>
    setStates((s) => (on ? [...s, state] : s.filter((x) => x !== state)));

  const label =
    count === undefined
      ? "Counting…"
      : count === 0
        ? "Nothing to archive"
        : `Archive ${count} ${count === 1 ? "identity" : "identities"}`;

  return (
    <div className="overlay">
      <div className="dialog">
        <h2>Archive old identities</h2>
        <p className="muted small">
          Archived lost and gone identities leave lists and aggregates. Nothing is deleted; rows
          stay readable by id.
        </p>
        <fieldset>
          <legend>States</legend>
          {(["lost", "gone"] as const).map((state) => (
            <label className="check" key={state}>
              <input
                type="checkbox"
                checked={states.includes(state)}
                onChange={(e) => toggle(state, e.target.checked)}
              />
              {state}
            </label>
          ))}
        </fieldset>
        <label>
          Ended more than (days ago)
          <input
            type="number"
            min={0}
            placeholder="use server default"
            value={olderThanDays}
            onChange={(e) => setOlderThanDays(e.target.value)}
          />
        </label>
        {error && <p className="error">{error}</p>}
        <div className="actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="primary" disabled={busy || !count} onClick={confirm}>
            {label}
          </button>
        </div>
      </div>
    </div>
  );
}
