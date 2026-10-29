import { useEffect, useId, useState } from "react";
import { type ArgusEvent, api, call } from "../api";
import { describeEvent, isTransientEvent } from "../eventLine";
import { relative, shortId } from "../format";
import { useLive } from "../live";
import { useNow } from "./Activity";
import { EventList } from "./EventList";

const SEED_LIMIT = 100;
const MAX = 100;

/**
 * Fixed live status (live work spec 3.4): stream health + latest event on the
 * strip; click expands to a half-viewport scrollable feed.
 */
export function LiveStatusBar() {
  const { status, subscribe } = useLive();
  const now = useNow();
  const feedId = useId();
  const [events, setEvents] = useState<ArgusEvent[]>([]);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let seeded = false;
    const early: ArgusEvent[] = [];

    const take = (next: ArgusEvent) => {
      if (isTransientEvent(next)) return;
      if (!seeded) {
        early.push(next);
        return;
      }
      setEvents((list) => [next, ...list].slice(0, MAX));
    };

    const unsubscribe = subscribe(take);
    call(api.GET("/api/events", { params: { query: { limit: SEED_LIMIT } } })).then(
      (history) => {
        if (cancelled) return;
        const clean = history.filter((e) => !isTransientEvent(e));
        const seen = new Set(clean.map((e) => e.id));
        const fromStream = early.filter((e) => !isTransientEvent(e) && !seen.has(e.id));
        setEvents([...fromStream.reverse(), ...clean].slice(0, MAX));
        seeded = true;
      },
      () => {
        if (cancelled) return;
        seeded = true;
        const fromStream = early.filter((e) => !isTransientEvent(e));
        if (fromStream.length) setEvents(fromStream.reverse().slice(0, MAX));
      },
    );

    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [subscribe]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const latest = events[0];
  const detail = latest
    ? [
        relative(latest.at, now),
        latest.kind,
        `${latest.subject_type} ${shortId(latest.subject_id)}`,
        describeEvent(latest),
      ]
        .filter(Boolean)
        .join(" · ")
    : "Waiting for events…";

  return (
    <div className={`live-status${open ? " open" : ""}`}>
      <div
        id={feedId}
        className="live-status-feed"
        role="region"
        aria-label="Recent events"
        aria-hidden={!open}
        inert={open ? undefined : true}
      >
        <div className="live-status-feed-head">
          <span className="small">Recent events</span>
          <a href="#events" className="small">
            All events
          </a>
        </div>
        <EventList events={events} />
      </div>
      <button
        type="button"
        className="live-status-strip"
        aria-label="Live status"
        aria-expanded={open}
        aria-controls={feedId}
        onClick={() => setOpen((v) => !v)}
      >
        <span className={`status status-${status}`} title={`Event stream: ${status}`}>
          {status}
        </span>
        <span className="live-status-line">{detail}</span>
      </button>
    </div>
  );
}
