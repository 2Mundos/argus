import type { ArgusEvent } from "../api";
import { describeEvent } from "../eventLine";
import { actor, relative, shortId } from "../format";

export function EventList({ events }: { events: ArgusEvent[] }) {
  if (events.length === 0) return <p className="muted">No events.</p>;
  return (
    <ol className="events">
      {events.map((e) => (
        <li key={e.id}>
          <time dateTime={e.at} title={e.at}>
            {relative(e.at)}
          </time>
          <span className="badge">{e.kind}</span>
          <span className="small">
            {e.subject_type} {shortId(e.subject_id)}
          </span>
          <span className="muted small">{describeEvent(e)}</span>
          <span className="muted small actor">{actor(e.actor)}</span>
        </li>
      ))}
    </ol>
  );
}
