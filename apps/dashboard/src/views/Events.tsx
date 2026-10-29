import { useEffect, useState } from "react";
import { type ArgusEvent, api, call } from "../api";
import { actor, clock, relative, shortId } from "../format";
import { useLive } from "../live";

const MAX = 300;

export function Events() {
  const { subscribe } = useLive();
  const [events, setEvents] = useState<ArgusEvent[]>([]);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    let loaded = false;
    const early: ArgusEvent[] = [];
    // Subscribe first, then load history, so nothing slips in between.
    const unsubscribe = subscribe((event) => {
      if (event.kind === "task.log") return; // transient output, not history
      if (!loaded) early.push(event);
      else setEvents((list) => [event, ...list].slice(0, MAX));
    });
    call(api.GET("/api/events", { params: { query: { limit: 200 } } })).then((history) => {
      const seen = new Set(history.map((e) => e.id));
      setEvents([...early.filter((e) => !seen.has(e.id)).reverse(), ...history]);
      loaded = true;
    });
    return () => {
      unsubscribe();
    };
  }, [subscribe]);

  const shown = filter ? events.filter((e) => e.subject_type === filter) : events;
  return (
    <section>
      <div className="toolbar">
        <h2>Events</h2>
        <select value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">All subjects</option>
          <option value="task">Tasks</option>
          <option value="agent">Agents</option>
          <option value="decision">Decisions</option>
          <option value="task_kind">Task kinds</option>
        </select>
      </div>
      <p className="page-lead">The latest changes, newest first.</p>
      <EventList events={shown} />
    </section>
  );
}

export function EventList({ events }: { events: ArgusEvent[] }) {
  if (events.length === 0) return <p className="muted empty-state">No events.</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Type</th>
            <th>Details</th>
            <th>Status</th>
            <th>Agent</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.id}>
              <td>
                <time dateTime={e.at} title={relative(e.at)}>
                  {clock(e.at)}
                </time>
              </td>
              <td>{e.subject_type}</td>
              <td>
                {e.kind}
                {describe(e) ? ` · ${describe(e)}` : ""} {shortId(e.subject_id)}
              </td>
              <td>
                <span className={`badge ${eventTone(e.kind)}`}>{e.kind.split(".").pop()}</span>
              </td>
              <td className="muted small">{actor(e.actor)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function eventTone(kind: string): string {
  if (/fail|error|lost|cancel/.test(kind)) return "state-failed";
  if (/warn|degrad|block|expir/.test(kind)) return "state-blocked";
  if (/succeed|created|ready|ok/.test(kind)) return "state-succeeded";
  return "";
}

function describe(e: ArgusEvent): string {
  const d = e.data as Record<string, unknown>;
  if (typeof d.from === "string" && typeof d.to === "string") return `${d.from} → ${d.to}`;
  if (typeof d.message === "string") return d.message;
  if (typeof d.reason === "string") return d.reason;
  if (typeof d.to === "string") return `→ ${d.to}`;
  return "";
}
