import { useEffect, useState } from "react";
import { api, type TaskChanges as Changes, call } from "../api";
import { DateChip } from "../DateChip";
import { useLive } from "../live";
import {
  changesEvent,
  lineKind,
  OPEN_FILES,
  shortSha,
  snapshotAttempts,
  splitPatch,
} from "../taskChanges";

export function useTaskChangesSnapshot({
  taskId,
  attempt,
  selected,
}: {
  taskId: string;
  attempt: number;
  selected?: number;
}) {
  const { subscribe, connection } = useLive();
  const [own, setShown] = useState(attempt);
  const shown = selected ?? own;
  const [changes, setChanges] = useState<Changes | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<string>();
  const [version, setVersion] = useState(0);

  useEffect(() => setShown(attempt), [attempt]);

  useEffect(
    () =>
      subscribe((event) => {
        if (changesEvent(event, taskId) === shown) setVersion((v) => v + 1);
      }),
    [subscribe, taskId, shown],
  );

  useEffect(() => {
    let cancelled = false;
    call(
      api.GET("/api/tasks/{task_id}/changes", {
        params: { path: { task_id: taskId }, query: { attempt: shown } },
      }),
    ).then(
      (got) => {
        if (cancelled) return;
        setChanges(got);
        setMissing(false);
        setError(undefined);
      },
      (err) => {
        if (cancelled) return;
        if ((err as { status?: number })?.status === 404) {
          setChanges(null);
          setMissing(true);
          setError(undefined);
        } else setError(String(err.message ?? err));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [taskId, shown, version, connection]);

  const files = changes?.files ?? [];
  const added = files.reduce((n, f) => n + (f.added ?? 0), 0);
  const deleted = files.reduce((n, f) => n + (f.deleted ?? 0), 0);

  return {
    shown,
    setShown,
    changes,
    missing,
    error,
    files,
    added,
    deleted,
    patches: splitPatch(changes?.patch ?? ""),
    attempts: snapshotAttempts(attempt),
    commits: changes?.commits ?? [],
    untracked: changes?.untracked ?? [],
    cut: changes?.truncated ?? { files: false, patch: false },
  };
}

export function TaskChangesStatus({ snap }: { snap: ReturnType<typeof useTaskChangesSnapshot> }) {
  const { changes, files, added, deleted } = snap;
  if (!changes) return null;
  return (
    <span className="term-dim term-changes-status">
      {changes.branch} @ {shortSha(changes.head)} ·{" "}
      {`${files.length} file${files.length === 1 ? "" : "s"} `}
      <span className="diff-add">{`+${added}`}</span>{" "}
      <span className="diff-del">{`−${deleted}`}</span>
    </span>
  );
}

export function TaskChangesTabCounts({
  snap,
}: {
  snap: ReturnType<typeof useTaskChangesSnapshot>;
}) {
  const { changes, files, added, deleted } = snap;
  if (!changes) return "Changes";
  const fileWord = files.length === 1 ? "file" : "files";
  return (
    <>
      Changes{" "}
      <span className="term-tab-count">
        · {files.length} {fileWord} <span className="diff-add">{`+${added}`}</span>{" "}
        <span className="diff-del">{`−${deleted}`}</span>
      </span>
    </>
  );
}

function TaskChangesBody({ snap }: { snap: ReturnType<typeof useTaskChangesSnapshot> }) {
  const { changes, missing, error, files, patches, commits, untracked, cut } = snap;

  return (
    <>
      {error && <p className="term-error">{error}</p>}
      <div className="term-body changes-body">
        {!changes ? (
          <span className="term-empty">
            {missing ? "No changes reported for this attempt yet." : "Loading…"}
          </span>
        ) : (
          <>
            {(cut.files || cut.patch) && (
              <p className="changes-cut">
                Cut to fit: {cut.files ? "not every file is listed" : ""}
                {cut.files && cut.patch ? "; " : ""}
                {cut.patch ? "the patch is incomplete" : ""}.
              </p>
            )}
            <p className="term-dim">
              {changes.base
                ? `against ${changes.base.ref} (${shortSha(changes.base.sha)})`
                : "against the empty tree (nothing fetched yet)"}
              {" · "}
              <DateChip at={changes.at} />
            </p>
            {commits.length > 0 && (
              <ul className="changes-commits">
                {commits.map((c) => (
                  <li key={c.sha}>
                    <span className="term-dim">{shortSha(c.sha)}</span> {c.subject}
                  </li>
                ))}
              </ul>
            )}
            <ul className="changes-files">
              {files.map((f) => (
                <li key={f.path}>
                  <span className="changes-status">{f.status}</span> {f.path}{" "}
                  {f.binary ? (
                    <span className="term-dim">binary</span>
                  ) : (
                    <>
                      <span className="diff-add">{`+${f.added ?? 0}`}</span>{" "}
                      <span className="diff-del">{`−${f.deleted ?? 0}`}</span>
                    </>
                  )}
                </li>
              ))}
              {untracked.map((u) => (
                <li key={`?${u.path}`}>
                  <span className="changes-status">?</span> {u.path}{" "}
                  <span className="term-dim">untracked, not in the diff</span>
                </li>
              ))}
            </ul>
            {patches.map((p, i) => (
              <details key={`${i}:${p.path}`} open={i < OPEN_FILES} className="changes-patch">
                <summary>{p.path}</summary>
                <pre>
                  {p.text.split("\n").map((line, n) => (
                    <div key={n} className={`diff-${lineKind(line)}`}>
                      {line || " "}
                    </div>
                  ))}
                </pre>
              </details>
            ))}
          </>
        )}
      </div>
    </>
  );
}

/** What the attempt changed in its workspace: the latest snapshot, refreshed on `task.changes`. */
export function TaskChanges({
  taskId,
  attempt,
  hideWhenEmpty = false,
  selected,
  embedded = false,
  snap: externalSnap,
}: {
  taskId: string;
  attempt: number;
  selected?: number;
  hideWhenEmpty?: boolean;
  embedded?: boolean;
  /** Reuse a snapshot loaded by a parent (tabbed terminal). */
  snap?: ReturnType<typeof useTaskChangesSnapshot>;
}) {
  const internalSnap = useTaskChangesSnapshot({ taskId, attempt, selected });
  const snap = externalSnap ?? internalSnap;
  const { shown, setShown, changes, missing, files, added, deleted, attempts } = snap;

  if (hideWhenEmpty && missing && shown === attempt) return null;

  if (embedded) return <TaskChangesBody snap={snap} />;

  return (
    <section className="term task-changes" aria-label="Changes">
      <div className="term-bar">
        <span className="term-traffic" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        <span className="term-title">
          {changes ? (
            <>
              changes · {changes.branch} @ {shortSha(changes.head)} · <DateChip at={changes.at} />
            </>
          ) : (
            "changes"
          )}
        </span>
        {selected === undefined && attempts.length > 1 && (
          <select
            className="term-filter"
            aria-label="Attempt"
            value={shown}
            onChange={(e) => setShown(Number(e.target.value))}
          >
            {attempts.map((a) => (
              <option key={a} value={a}>
                {`attempt ${a}`}
              </option>
            ))}
          </select>
        )}
        {changes && (
          <span className="term-dim">
            {`${files.length} file${files.length === 1 ? "" : "s"} `}
            <span className="diff-add">{`+${added}`}</span>{" "}
            <span className="diff-del">{`−${deleted}`}</span>
          </span>
        )}
      </div>
      <TaskChangesBody snap={snap} />
    </section>
  );
}
