import type { ArgusEvent } from "../api";
import { DateChip } from "../DateChip";
import { describeEvent } from "../eventLine";
import { shortId } from "../format";
import { WorkerActorChip } from "../WorkerChip";

function eventDetail(event: Pick<ArgusEvent, "data" | "subject_type" | "subject_id">): string {
  const summary = describeEvent(event);
  const subject = `${event.subject_type} ${shortId(event.subject_id)}`;
  return summary ? `${summary} · ${subject}` : subject;
}

export function EventList({ events }: { events: ArgusEvent[] }) {
  if (events.length === 0) return <p className="muted empty-state">No events.</p>;
  return (
    <div className="table-wrap events-table">
      <table>
        <thead>
          <tr>
            <th>Time</th>
            <th>Type</th>
            <th>Details</th>
            <th>Status</th>
            <th>Worker</th>
          </tr>
        </thead>
        <tbody>
          {events.map((e) => (
            <tr key={e.id}>
              <td>
                <DateChip at={e.at} />
              </td>
              <td>{e.kind}</td>
              <td>{eventDetail(e)}</td>
              <td>
                <span className={`badge ${eventTone(e.kind)}`}>{e.kind.split(".").pop()}</span>
              </td>
              <td className="muted small">
                <WorkerActorChip actor={e.actor} />
              </td>
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
