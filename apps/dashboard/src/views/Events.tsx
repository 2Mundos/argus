import { useEffect, useState } from "react";
import { type ArgusEvent, api, call } from "../api";
import { isTransientEvent } from "../eventLine";
import { useLive } from "../live";
import { PageHeader } from "../PageHeader";
import { EventList } from "./EventList";

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
      if (isTransientEvent(event)) return;
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
      <PageHeader title="Events" lead="The latest changes, newest first.">
        <select value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="">All subjects</option>
          <option value="task">Tasks</option>
          <option value="agent">Agents</option>
          <option value="decision">Decisions</option>
          <option value="task_kind">Task kinds</option>
        </select>
      </PageHeader>
      <EventList events={shown} />
    </section>
  );
}
