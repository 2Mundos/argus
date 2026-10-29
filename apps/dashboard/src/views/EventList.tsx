import type { ArgusEvent } from "../api";
import { describeEvent } from "../eventLine";
import { actor, clock, relative, shortId } from "../format";

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
                {describeEvent(e) ? ` · ${describeEvent(e)}` : ""} {shortId(e.subject_id)}
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
