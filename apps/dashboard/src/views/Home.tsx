import { useEffect, useState } from "react";
import { api, call } from "../api";
import { relative } from "../format";
import { useLive, useLiveData } from "../live";
import {
  type Attention,
  attention,
  type Counts,
  type Overview,
  type Source,
  sourceStatus,
  sum,
  type Verdict,
  verdict,
} from "../overview";
import { EventList } from "./Events";
import { SystemWidgets } from "./SystemWidgets";

function useSource<T>(
  load: (signal: AbortSignal) => Promise<T>,
): Source<T> & { reload: () => void } {
  const { data, error, reload } = useLiveData((signal) => load(signal), []);
  const [updatedAt, setUpdatedAt] = useState<number>();
  useEffect(() => {
    if (data !== undefined) setUpdatedAt(Date.now());
  }, [data]);
  return { data, error: error?.message, updatedAt, reload };
}

const fmt = (n: number | undefined) => (n === undefined ? "—" : String(n));

export function Home() {
  const { status } = useLive();
  const overview = useSource<Overview>((signal) => call(api.GET("/api/overview", { signal })));
  const system = useSource((signal) => call(api.GET("/api/system", { signal })));
  const events = useSource((signal) =>
    call(api.GET("/api/events", { params: { query: { limit: 10 } }, signal })),
  );
  const live = status === "live";
  const att = attention({ stream: status, overview, system });
  const o = overview.data;
  const v = verdict(system.data);

  return (
    <section className="home">
      <div className="toolbar">
        <h2>Home</h2>
        <StatusBar
          verdict={v}
          stream={status}
          overview={sourceStatus(overview, live)}
          updatedAt={overview.updatedAt}
        />
      </div>

      <h3>Needs attention</h3>
      <AttentionList att={att} />
      {overview.error !== undefined && (
        <p className="error" role="alert">
          Totals failed to load: {overview.error}{" "}
          <button type="button" onClick={overview.reload}>
            Retry
          </button>
        </p>
      )}

      <h3>Summary</h3>
      {o === undefined ? (
        <p className="muted">{overview.error ? "Totals unavailable." : "Loading totals…"}</p>
      ) : (
        <div className="home-groups">
          <Group title="Tasks" href="#tasks">
            <Stat label="Awaiting approval" value={o.tasks.awaiting_approval} />
            <Stat label="Ready" value={o.tasks.ready} />
            <Stat label="Claimed" value={o.tasks.claimed} />
            <Stat label="Blocked" value={o.tasks.blocked} />
            <Stat label="In review" value={o.tasks.in_review} />
            <Stat label="Failed" value={o.tasks.failed} />
          </Group>
          <Group title="Agents" href="#workers/agents">
            <Stat label="Active" value={o.agents.by_state.active} />
            <Stat label="Draining" value={o.agents.by_state.draining} />
            <Stat label="Lost" value={o.agents.by_state.lost} />
            <Stat label="Holding claims" value={o.agents.holding_claims} />
            <Stat label="Lost holding claims" value={o.agents.lost_holding_claims} />
          </Group>
          <Group title="Pools" href="#workers/pools">
            <Stat label="Pools" value={o.pools.total} />
            <Stat label="Running" value={`${o.pools.running} / ${o.pools.desired}`} />
            <Stat label="Live" value={`${o.pools.live} / ${o.pools.max}`} />
          </Group>
          <Group title="Inbox" href="#inbox">
            <Stat label="Open decisions" value={o.decisions.by_state.open} />
            <Stat label="Open proposals" value={o.knowledge.proposals_by_state.open} />
          </Group>
          <Group title="Accounts" href="#setup/accounts">
            <Stat label="Available" value={o.accounts.by_state.available} />
            <Stat label="Exhausted" value={o.accounts.by_state.exhausted} />
            <Stat label="Total" value={o.accounts.total} />
            <p className="muted small">
              Quota is not measured by Argus.
              {o.accounts.next_reset_at && ` Next reset ${relative(o.accounts.next_reset_at)}.`}
            </p>
          </Group>
        </div>
      )}

      <h3>Recent activity</h3>
      {events.data !== undefined ? (
        <EventList events={events.data} />
      ) : events.error !== undefined ? (
        <p className="error" role="alert">
          Activity failed to load: {events.error}{" "}
          <button type="button" onClick={events.reload}>
            Retry
          </button>
        </p>
      ) : (
        <p className="muted">Loading activity…</p>
      )}
      <p className="small">
        <a href="#setup/events">All events</a>
      </p>

      {system.data === undefined ? (
        <p className={system.error ? "error" : "muted"}>
          {system.error ? `System health unavailable: ${system.error}` : "Loading system…"}
        </p>
      ) : (
        <>
          <SystemWidgets data={system.data} />
          <p className="muted small">
            Heartbeats and running state show that processes are alive, not that a model answers.
          </p>
        </>
      )}

      <details className="home-details">
        <summary>All states</summary>
        {o === undefined ? (
          <p className="muted">Unavailable until totals load.</p>
        ) : (
          <AllStates o={o} />
        )}
      </details>
    </section>
  );
}

function StatusBar(props: {
  verdict: Verdict;
  stream: string;
  overview: string;
  updatedAt?: number;
}) {
  const text: Record<Verdict, string> = {
    ok: "Checks passing",
    degraded: "Checks degraded",
    failing: "Checks failing",
    unknown: "Health unknown",
  };
  return (
    <span className="home-status small" role="status">
      <span className={`badge verdict-${props.verdict}`}>{text[props.verdict]}</span>
      <span className="muted">
        Stream {props.stream}
        {props.overview === "stale" && " · totals may be stale"}
        {props.updatedAt !== undefined &&
          ` · updated ${relative(new Date(props.updatedAt).toISOString())}`}
      </span>
    </span>
  );
}

const ICON = { critical: "✖", attention: "▲", info: "ℹ" } as const;

function AttentionList({ att }: { att: Attention }) {
  const loading = att.pending.length > 0;
  return (
    <div aria-live="polite">
      {att.items.length > 0 && (
        <ul className="attention">
          {att.items.map((i) => (
            <li key={i.id} className={`sev-${i.severity}`}>
              <span aria-hidden="true">{ICON[i.severity]}</span>
              <span className="sev-label">{i.severity}</span>
              <span>{i.text}</span>
              {i.href && <a href={i.href}>View</a>}
            </li>
          ))}
        </ul>
      )}
      {loading && <p className="muted">Loading… ({att.pending.join(", ")})</p>}
      {!loading && att.unavailable.length === 0 && att.items.length === 0 && (
        <p className="muted">Nothing needs attention right now.</p>
      )}
      {!loading && att.unavailable.length > 0 && (
        <p className="muted small">Not an all-clear: some sources could not be read.</p>
      )}
    </div>
  );
}

function Group(props: { title: string; href: string; children: React.ReactNode }) {
  return (
    <div className="card home-group">
      <h4>
        <a href={props.href}>{props.title}</a>
      </h4>
      <div className="stats">{props.children}</div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number | string | undefined }) {
  return (
    <div className="stat">
      <span className="n">{value === undefined ? "—" : value}</span>
      <span className="muted small">{label}</span>
    </div>
  );
}

function CountsTable({ title, counts }: { title: string; counts: Counts }) {
  return (
    <div className="card">
      <h4>{title}</h4>
      <dl className="counts">
        {Object.entries(counts).map(([k, n]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{n}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function AllStates({ o }: { o: Overview }) {
  const total = sum(o.tasks, Object.keys(o.tasks));
  return (
    <div className="home-groups">
      <CountsTable title={`Tasks (${fmt(total)} total)`} counts={o.tasks} />
      <CountsTable title="Agents" counts={o.agents.by_state} />
      {Object.entries(o.agents.by_role).map(([role, c]) => (
        <CountsTable key={role} title={`Agents · ${role}`} counts={c} />
      ))}
      <CountsTable title="Pools" counts={o.pools.by_status} />
      <CountsTable title="Instances" counts={o.instances.by_state} />
      <CountsTable title="Accounts" counts={o.accounts.by_state} />
      {Object.entries(o.accounts.by_provider).map(([p, c]) => (
        <CountsTable key={p} title={`Accounts · ${p}`} counts={c} />
      ))}
      <CountsTable title="Decisions" counts={o.decisions.by_state} />
      <CountsTable title="Open decisions by kind" counts={o.decisions.open_by_kind} />
      <CountsTable title="Open decisions by audience" counts={o.decisions.open_by_audience} />
      <CountsTable title="Knowledge pages" counts={o.knowledge.pages_by_state} />
      <CountsTable title="Knowledge proposals" counts={o.knowledge.proposals_by_state} />
      <p className="muted small">
        Lost and terminated counts are history; only lost agents on instances not yet terminated are
        flagged above.
      </p>
    </div>
  );
}
