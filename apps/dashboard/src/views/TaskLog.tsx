import { useEffect, useRef, useState } from "react";
import { api, call } from "../api";
import { useLive } from "../live";
import { filterLines, type LogFilter, type LogLine, logEvent, mergeLines } from "../taskLog";

const FILTERS: [LogFilter, string][] = [
  ["all", "Tudo"],
  ["worker", "Worker"],
  ["delegate", "Delegate"],
];

/** The "Ao vivo" panel: the attempt's log tail, then its `task.log` events as they arrive. */
export function TaskLog({ taskId, attempt }: { taskId: string; attempt: number }) {
  const { subscribe } = useLive();
  const [lines, setLines] = useState<LogLine[]>([]);
  const [filter, setFilter] = useState<LogFilter>("all");
  const [pausedAt, setPausedAt] = useState<number | null>(null); // last seq shown while paused
  const [error, setError] = useState<string>();
  const box = useRef<HTMLPreElement>(null);
  const atEnd = useRef(true); // the reader is at the end: keep following

  useEffect(() => {
    let loaded = false;
    const early: LogLine[] = [];
    setLines([]);
    setError(undefined);
    // Subscribe first, then backfill, so nothing slips in between; merging dedupes by seq.
    const unsubscribe = subscribe((event) => {
      const got = logEvent(event, taskId);
      if (!got || got.attempt !== attempt) return;
      if (loaded) setLines((have) => mergeLines(have, got.lines));
      else early.push(...got.lines);
    });
    call(
      api.GET("/api/tasks/{task_id}/log", {
        params: { path: { task_id: taskId }, query: { attempt } },
      }),
    ).then(
      (page) => {
        loaded = true;
        setLines(mergeLines(mergeLines([], page.lines), early));
      },
      (err: Error) => {
        loaded = true;
        setError(err.message);
      },
    );
    return unsubscribe;
  }, [subscribe, taskId, attempt]);

  const shown = filterLines(
    pausedAt == null ? lines : lines.filter((l) => l.seq <= pausedAt),
    filter,
  );
  const behind = pausedAt == null ? 0 : lines.filter((l) => l.seq > pausedAt).length;

  useEffect(() => {
    if (pausedAt == null && atEnd.current && box.current)
      box.current.scrollTop = box.current.scrollHeight;
  }, [shown.length, pausedAt]);

  return (
    <section className="task-log" aria-label="Ao vivo">
      <div className="toolbar">
        <h3>Ao vivo</h3>
        <span className="muted small">tentativa {attempt}</span>
        <select
          aria-label="Origem"
          value={filter}
          onChange={(e) => setFilter(e.target.value as LogFilter)}
        >
          {FILTERS.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => setPausedAt(pausedAt == null ? (lines.at(-1)?.seq ?? 0) : null)}
        >
          {pausedAt == null ? "Pausar" : `Retomar${behind ? ` (+${behind})` : ""}`}
        </button>
      </div>
      {error && <p className="error">{error}</p>}
      <pre
        ref={box}
        className="log"
        tabIndex={0}
        onScroll={(e) => {
          const el = e.currentTarget;
          atEnd.current = el.scrollHeight - el.scrollTop - el.clientHeight <= 8;
        }}
      >
        {shown.length === 0 ? (
          <span className="muted">Sem saída ainda.</span>
        ) : (
          shown.map((l) => (
            <div key={l.seq} className={`log-line ${l.source}`}>
              <span className="muted">{l.source === "delegate" ? "[delegate] " : ""}</span>
              {l.line}
            </div>
          ))
        )}
      </pre>
    </section>
  );
}
