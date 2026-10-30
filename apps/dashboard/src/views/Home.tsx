import { type ReactNode, useEffect, useState } from "react";
import { api, call } from "../api";
import { DateChip } from "../DateChip";
import { useLive, useLiveData } from "../live";
import {
  type Attention,
  attention,
  type Counts,
  type Overview,
  type Source,
  sum,
} from "../overview";
import { PageHeader } from "../PageHeader";
import { EventList } from "./EventList";
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
  const att = attention({ stream: status, overview, system });
  const o = overview.data;
  const [panel, setPanel] = useState<HomePanel>("overview");

  return (
    <section className="home">
      <PageHeader title="Home" lead="Monitor agents, tasks and the fleet from one place.">
        <div className="home-tabs" role="tablist" aria-label="Home sections">
          {HOME_PANELS.map(([id, label]) => (
            <button
              key={id}
              type="button"
              role="tab"
              id={`home-tab-${id}`}
              aria-selected={panel === id}
              aria-controls={`home-panel-${id}`}
              onClick={() => setPanel(id)}
            >
              {label}
            </button>
          ))}
        </div>
      </PageHeader>

      {panel === "overview" && (
        <div
          className="home-panel"
          role="tabpanel"
          id="home-panel-overview"
          aria-labelledby="home-tab-overview"
        >
          {o === undefined ? (
            <p className="muted">{overview.error ? "Totals unavailable." : "Loading totals…"}</p>
          ) : (
            <div className="stat-row">
              <BreakdownCard
                href="#tasks"
                tone="tasks"
                title="Tasks"
                counts={o.tasks}
                icon="M9 6h11M9 12h11M9 18h11M5 6h.01M5 12h.01M5 18h.01"
              />
              <BreakdownCard
                href="#workers/agents"
                tone="agents"
                title="Agents"
                counts={o.agents.by_state}
                icon="M16 19v-1a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v1M9.5 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6"
              />
              <BreakdownCard
                href="#workers/pools"
                tone="pools"
                title="Pools"
                counts={o.pools.by_status}
                icon="M12 3 4 7l8 4 8-4zM4 12l8 4 8-4M4 17l8 4 8-4"
              />
              <BreakdownCard
                href="#inbox"
                tone="inbox"
                title="Inbox"
                counts={o.decisions.by_state}
                icon="M4 5h16v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2zM4 5l8 6 8-6"
              />
              <BreakdownCard
                href="#setup/accounts"
                tone="accounts"
                title="Accounts"
                counts={o.accounts.by_state}
                icon="M15 7a3 3 0 1 1-6 0 3 3 0 0 1 6 0M5 19a6 6 0 0 1 14 0"
                note={
                  <>
                    Quota is not measured by Argus.
                    {o.accounts.next_reset_at ? (
                      <>
                        {" "}
                        Next reset <DateChip at={o.accounts.next_reset_at} />.
                      </>
                    ) : null}
                  </>
                }
              />
            </div>
          )}
          <AttentionList att={att} />
          {overview.error !== undefined && (
            <p className="error" role="alert">
              Totals failed to load: {overview.error}{" "}
              <button type="button" onClick={overview.reload}>
                Retry
              </button>
            </p>
          )}
          <div className="home-split">
            <div className="card">
              <div className="toolbar">
                <h3>Recent activity</h3>
                <a href="#setup/events">All events</a>
              </div>
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
            </div>
            <div className="card">
              <h3>Quick actions</h3>
              <div className="home-actions">
                <a href="#tasks">
                  <Stroke d="M12 5v14M5 12h14" /> Create task
                </a>
                <a href="#workers/agents">
                  <Stroke d="M16 19v-1a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4v1M9.5 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6" />{" "}
                  Add agent
                </a>
                <a href="#workers/pools">
                  <Stroke d="M12 3 4 7l8 4 8-4zM4 12l8 4 8-4" /> Manage pools
                </a>
                <a href="#inbox">
                  <Stroke d="M4 5h16v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2zM4 5l8 6 8-6" /> Check inbox
                </a>
                <a href="#setup/events">
                  <Stroke d="M4 6h16M4 12h10M4 18h13" /> View logs
                </a>
              </div>
            </div>
          </div>
        </div>
      )}

      {panel === "system" && (
        <div
          className="home-panel"
          role="tabpanel"
          id="home-panel-system"
          aria-labelledby="home-tab-system"
        >
          {system.data === undefined ? (
            <p className={system.error ? "error" : "muted"}>
              {system.error ? `System health unavailable: ${system.error}` : "Loading system…"}
            </p>
          ) : (
            <>
              <SystemWidgets data={system.data} />
            </>
          )}
          <details className="home-details" open>
            <summary>All states</summary>
            {o === undefined ? (
              <p className="muted">Unavailable until totals load.</p>
            ) : (
              <AllStates o={o} />
            )}
          </details>
        </div>
      )}
    </section>
  );
}

const HOME_PANELS = [
  ["overview", "Overview"],
  ["system", "System"],
] as const;

type HomePanel = (typeof HOME_PANELS)[number][0];

const SEVERITIES = ["critical", "attention", "info"] as const;

function AttentionList({ att }: { att: Attention }) {
  const [open, setOpen] = useState(false);
  const loading = att.pending.length > 0;
  if (!loading && att.items.length === 0 && att.unavailable.length === 0) return null;
  const kinds = SEVERITIES.map((kind) => ({
    kind,
    count: att.items.filter((item) => item.severity === kind).length,
  })).filter((row) => row.count > 0);
  return (
    <div className={`card attention-card${open ? " is-open" : ""}`} aria-live="polite">
      <div className="toolbar">
        <h3>Needs attention</h3>
        {kinds.length > 0 && (
          <span className="attention-kinds">
            {kinds.map((row) => (
              <span key={row.kind} className={`attention-kind sev-${row.kind}`}>
                <span className={`sev-dot sev-${row.kind}`} aria-hidden="true" />
                {row.count} {row.kind}
              </span>
            ))}
          </span>
        )}
        {att.items.length > 0 && (
          <button
            type="button"
            aria-expanded={open}
            aria-controls="attention-items"
            onClick={() => setOpen((value) => !value)}
          >
            {open ? "Hide" : "Show"}
            <svg className="attention-caret" viewBox="0 0 24 24" aria-hidden="true">
              <path
                d="M9 6l6 6-6 6"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        )}
      </div>
      <p className="muted small">Items that need a person or an operator.</p>
      {open && att.items.length > 0 && (
        <ul id="attention-items" className="attention">
          {att.items.map((i) => (
            <li key={i.id} className={`sev-${i.severity}`}>
              <span className={`sev-dot sev-${i.severity}`} aria-hidden="true" />
              <span className="sev-label">{i.severity}</span>
              <span>{i.text}</span>
              {i.href && <a href={i.href}>View</a>}
            </li>
          ))}
        </ul>
      )}
      {loading && <p className="muted">Loading… ({att.pending.join(", ")})</p>}
      {!loading && att.unavailable.length > 0 && (
        <p className="muted small">Not an all-clear: some sources could not be read.</p>
      )}
    </div>
  );
}

function Stroke({ d }: { d: string }) {
  return (
    <svg className="nav-icon" viewBox="0 0 24 24" aria-hidden="true">
      <path
        d={d}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function stateLabel(key: string): string {
  const text = key.replaceAll("_", " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function BreakdownCard(props: {
  href: string;
  tone: string;
  title: string;
  counts: object;
  icon: string;
  note?: ReactNode;
}) {
  const rows = Object.entries(props.counts).filter(
    (entry): entry is [string, number] => typeof entry[1] === "number",
  );
  const total = rows.reduce((acc, [, n]) => acc + n, 0);
  return (
    <a className={`stat-card stat-${props.tone}`} href={props.href}>
      <span className="stat-head">
        <span className="stat-icon">
          <Stroke d={props.icon} />
        </span>
        <span className="stat-label">{props.title}</span>
        <span className="n">{total}</span>
      </span>
      <dl className="stat-breakdown">
        {rows.map(([key, n]) => (
          <div key={key}>
            <dt>{stateLabel(key)}</dt>
            <dd>{n}</dd>
          </div>
        ))}
      </dl>
      {props.note && <p className="muted small stat-note">{props.note}</p>}
    </a>
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
