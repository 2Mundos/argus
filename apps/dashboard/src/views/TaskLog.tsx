import { useEffect, useRef, useState } from "react";
import { api, call } from "../api";
import { useLive } from "../live";
import { filterLines, type LogFilter, type LogLine, logEvent, mergeLines } from "../taskLog";
import { TaskSteers } from "./TaskSteers";

const READ_LIMIT = 1000; // the API's largest page

const FILTERS: [LogFilter, string][] = [
  ["all", "All"],
  ["worker", "Worker"],
  ["delegate", "Delegate"],
];

export type TaskLogSteer = {
  claimed: boolean;
  assigneeId: string | null;
  supportsSteer?: boolean;
};

/** The live panel: the attempt's log tail, then its `task.log` events as they arrive. */
export function TaskLog({
  taskId,
  attempt,
  steer,
}: {
  taskId: string;
  attempt: number;
  /** When set, a Steer button sits next to Pause (task steering spec 3.9). */
  steer?: TaskLogSteer;
}) {
  const { subscribe, connection } = useLive();
  const [lines, setLines] = useState<LogLine[]>([]);
  const [filter, setFilter] = useState<LogFilter>("all");
  const [pausedAt, setPausedAt] = useState<number | null>(null); // last seq shown while paused
  const [steerOpen, setSteerOpen] = useState(false);
  const [error, setError] = useState<string>();
  const box = useRef<HTMLPreElement>(null);
  const atEnd = useRef(true); // the reader is at the end: keep following
  const catchUp = useRef(() => {});

  useEffect(() => {
    setSteerOpen(false);
  }, [taskId, attempt]);

  useEffect(() => {
    let cancelled = false;
    let loaded = false;
    let last = 0; // newest seq shown
    let filling = false;
    let refill: number | null = null; // a catch-up asked for while another runs
    const early: LogLine[] = [];
    setLines([]);
    setError(undefined);
    const add = (more: LogLine[]) => {
      for (const l of more) last = Math.max(last, l.seq);
      setLines((have) => mergeLines(have, more));
    };
    // Task.log events are transient: whatever the stream dropped (a reconnect, a
    // seq gap) is read back from the API, after the newest line held.
    const backfill = (after: number) => {
      if (filling) {
        refill = Math.min(refill ?? after, after);
        return;
      }
      filling = true;
      call(
        api.GET("/api/tasks/{task_id}/log", {
          params: { path: { task_id: taskId }, query: { attempt, after, limit: READ_LIMIT } },
        }),
      )
        .then(
          (page) => {
            if (cancelled) return;
            add(page.lines);
            if (page.lines.length === READ_LIMIT)
              refill = Math.min(refill ?? page.last_seq, page.last_seq);
          },
          () => {}, // the next reconnect or gap tries again
        )
        .finally(() => {
          filling = false;
          if (cancelled || refill == null) return;
          const next = refill;
          refill = null;
          backfill(next);
        });
    };
    catchUp.current = () => {
      if (loaded) backfill(last);
      else refill = 0; // the tail read may predate the stream: catch up after it
    };
    // Subscribe first, then backfill, so nothing slips in between; merging dedupes by seq.
    const unsubscribe = subscribe((event) => {
      const got = logEvent(event, taskId);
      if (!got || got.attempt !== attempt) return;
      if (!loaded) {
        early.push(...got.lines);
        return;
      }
      // seq counts across attempts: an empty tail's last_seq is the task's log_seq, so
      // last_seq + 1 is a no-gap start for any attempt
      const before = last;
      add(got.lines);
      if ((got.lines[0]?.seq ?? 0) > before + 1) backfill(before);
    });
    call(
      api.GET("/api/tasks/{task_id}/log", {
        params: { path: { task_id: taskId }, query: { attempt } },
      }),
    ).then(
      (page) => {
        if (cancelled) return;
        loaded = true;
        last = page.last_seq;
        for (const l of early) last = Math.max(last, l.seq);
        setLines(mergeLines(mergeLines([], page.lines), early));
        if (refill != null) {
          refill = null;
          backfill(page.last_seq);
        }
      },
      (err: Error) => {
        if (cancelled) return;
        loaded = true;
        setError(err.message);
      },
    );
    return () => {
      cancelled = true;
      catchUp.current = () => {};
      unsubscribe();
    };
  }, [subscribe, taskId, attempt]);

  const connected = useRef(connection);
  useEffect(() => {
    if (connection === connected.current) return;
    connected.current = connection;
    catchUp.current();
  }, [connection]);

  const shown = filterLines(
    pausedAt == null ? lines : lines.filter((l) => l.seq <= pausedAt),
    filter,
  );
  const behind = pausedAt == null ? 0 : lines.filter((l) => l.seq > pausedAt).length;

  useEffect(() => {
    if (pausedAt == null && atEnd.current && box.current)
      box.current.scrollTop = box.current.scrollHeight;
  }, [shown.length, pausedAt]);

  const paused = pausedAt != null;
  return (
    <section className={`term task-log${paused ? " term-paused" : ""}`} aria-label="Live">
      <div className="term-bar">
        <span className="term-traffic" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        <span className="term-title">{`live · attempt ${attempt}`}</span>
        <select
          className="term-filter"
          aria-label="Source"
          value={filter}
          onChange={(e) => setFilter(e.target.value as LogFilter)}
        >
          {FILTERS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <span className={`term-pulse${paused ? "" : " on"}`} title={paused ? "Paused" : "Live"}>
          {paused ? "paused" : "live"}
        </span>
        <button
          type="button"
          className="term-action"
          onClick={() => setPausedAt(paused ? null : (lines.at(-1)?.seq ?? 0))}
        >
          {paused ? `Resume${behind ? ` (+${behind})` : ""}` : "Pause"}
        </button>
        {steer?.claimed && (
          <button
            type="button"
            className="term-action"
            aria-expanded={steerOpen}
            onClick={() => setSteerOpen((open) => !open)}
          >
            {steerOpen ? "Close steer" : "Steer"}
          </button>
        )}
      </div>
      {steer && (
        <TaskSteers
          taskId={taskId}
          attempt={attempt}
          claimed={steer.claimed}
          assigneeId={steer.assigneeId}
          supportsSteer={steer.supportsSteer}
          composerOpen={steerOpen}
          onComposerOpenChange={setSteerOpen}
        />
      )}
      {error && <p className="term-error">{error}</p>}
      <pre
        ref={box}
        className="term-body log"
        tabIndex={0}
        onScroll={(e) => {
          const el = e.currentTarget;
          atEnd.current = el.scrollHeight - el.scrollTop - el.clientHeight <= 8;
        }}
      >
        {shown.length === 0 ? (
          <span className="term-empty">No output yet.</span>
        ) : (
          shown.map((l) => (
            <div key={l.seq} className={`log-line ${l.source}`}>
              <span className="term-dim">{l.source === "delegate" ? "[delegate] " : ""}</span>
              {l.line}
            </div>
          ))
        )}
      </pre>
    </section>
  );
}
