import { type RefObject, useEffect, useRef, useState } from "react";
import { api, call } from "../api";
import { useLive } from "../live";
import { filterLines, type LogFilter, type LogLine, logEvent, mergeLines } from "../taskLog";
import { TaskSteers } from "./TaskSteers";

const READ_LIMIT = 1000; // the API's largest page

export const LOG_FILTERS: [LogFilter, string][] = [
  ["all", "All"],
  ["worker", "Worker"],
  ["delegate", "Delegate"],
];

export type TaskLogSteer = {
  claimed: boolean;
  assigneeId: string | null;
  supportsSteer?: boolean;
};

export type TaskLogState = {
  lines: LogLine[];
  ready: boolean;
  pausedAt: number | null;
  setPausedAt: (seq: number | null) => void;
  steerOpen: boolean;
  setSteerOpen: (open: boolean | ((open: boolean) => boolean)) => void;
  error: string | undefined;
  behind: number;
  paused: boolean;
  box: RefObject<HTMLPreElement | null>;
  atEnd: RefObject<boolean>;
};

export function useTaskLog(taskId: string, attempt: number): TaskLogState {
  const { subscribe, connection } = useLive();
  const [lines, setLines] = useState<LogLine[]>([]);
  const [ready, setReady] = useState(false);
  const [pausedAt, setPausedAt] = useState<number | null>(null);
  const [steerOpen, setSteerOpen] = useState(false);
  const [error, setError] = useState<string>();
  const box = useRef<HTMLPreElement>(null);
  const atEnd = useRef(true);
  const catchUp = useRef(() => {});

  useEffect(() => {
    setSteerOpen(false);
  }, [taskId, attempt]);

  useEffect(() => {
    let cancelled = false;
    let loaded = false;
    let last = 0;
    let filling = false;
    let refill: number | null = null;
    const early: LogLine[] = [];
    setLines([]);
    setReady(false);
    setError(undefined);
    setPausedAt(null);
    const add = (more: LogLine[]) => {
      for (const l of more) last = Math.max(last, l.seq);
      setLines((have) => mergeLines(have, more));
    };
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
          () => {},
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
      else refill = 0;
    };
    const unsubscribe = subscribe((event) => {
      const got = logEvent(event, taskId);
      if (!got || got.attempt !== attempt) return;
      if (!loaded) {
        early.push(...got.lines);
        return;
      }
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
        setReady(true);
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
        setReady(true);
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

  const behind = pausedAt == null ? 0 : lines.filter((l) => l.seq > pausedAt).length;

  return {
    lines,
    ready,
    pausedAt,
    setPausedAt,
    steerOpen,
    setSteerOpen,
    error,
    behind,
    paused: pausedAt != null,
    box,
    atEnd,
  };
}

export function TaskLogBarActions({
  log,
  steer,
  filter,
  onFilterChange,
  hideFilter = false,
}: {
  log: TaskLogState;
  steer?: TaskLogSteer;
  filter: LogFilter;
  onFilterChange?: (filter: LogFilter) => void;
  hideFilter?: boolean;
}) {
  const { lines, pausedAt, setPausedAt, steerOpen, setSteerOpen, behind, paused } = log;
  return (
    <>
      {!hideFilter && (
        <select
          className="term-filter"
          aria-label="Source"
          value={filter}
          onChange={(e) => onFilterChange?.(e.target.value as LogFilter)}
        >
          {LOG_FILTERS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      )}
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
    </>
  );
}

export function TaskLogContent({
  taskId,
  attempt,
  steer,
  log,
  filter,
}: {
  taskId: string;
  attempt: number;
  steer?: TaskLogSteer;
  log: TaskLogState;
  filter: LogFilter;
}) {
  const { pausedAt, steerOpen, setSteerOpen, error, box, atEnd, lines } = log;
  const shown = filterLines(
    pausedAt == null ? lines : lines.filter((l) => l.seq <= pausedAt),
    filter,
  );

  useEffect(() => {
    if (pausedAt == null && atEnd.current && box.current)
      box.current.scrollTop = box.current.scrollHeight;
  }, [shown.length, pausedAt, box, atEnd]);

  return (
    <>
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
    </>
  );
}

/** The live panel: the attempt's log tail, then its `task.log` events as they arrive. */
export function TaskLog({
  taskId,
  attempt,
  steer,
}: {
  taskId: string;
  attempt: number;
  steer?: TaskLogSteer;
}) {
  const [filter, setFilter] = useState<LogFilter>("all");
  const log = useTaskLog(taskId, attempt);

  return (
    <section className={`term task-log${log.paused ? " term-paused" : ""}`} aria-label="Live">
      <div className="term-bar">
        <span className="term-traffic" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        <span className="term-title">{`live · attempt ${attempt}`}</span>
        <TaskLogBarActions log={log} steer={steer} filter={filter} onFilterChange={setFilter} />
      </div>
      <TaskLogContent taskId={taskId} attempt={attempt} steer={steer} log={log} filter={filter} />
    </section>
  );
}
