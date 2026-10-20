import { useEffect, useState } from "react";
import { api, call } from "../api";
import { ARCHIVABLE, type ArchiveCriteria, bulkBody, confirmLabel } from "../archive";
import { useLiveData } from "../live";

/** Archive terminal tasks in bulk: criteria, a dry-run count, then confirmation. */
export function ArchiveTasks({ onClose }: { onClose: () => void }) {
  const { data: projects } = useLiveData(() => call(api.GET("/api/projects")), []);
  const [criteria, setCriteria] = useState<ArchiveCriteria>({
    states: ["failed", "cancelled"],
    project: "",
    olderThanDays: "",
  });
  const [count, setCount] = useState<number>();
  const [error, setError] = useState<string>();
  const [busy, setBusy] = useState(false);

  // The count shown is always the server's dry run for exactly these criteria.
  useEffect(() => {
    setCount(undefined);
    const body = bulkBody(criteria, true);
    if (!body) {
      setCount(0);
      return;
    }
    let cancelled = false;
    call(api.POST("/api/tasks/archive", { body })).then(
      (result) => {
        if (!cancelled) setCount(result.count);
      },
      (err: Error) => {
        if (!cancelled) setError(err.message);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [criteria]);

  async function confirm() {
    const body = bulkBody(criteria, false);
    if (!body) return;
    setBusy(true);
    setError(undefined);
    try {
      await call(api.POST("/api/tasks/archive", { body }));
      onClose();
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  const toggle = (state: (typeof ARCHIVABLE)[number], on: boolean) =>
    setCriteria((c) => ({
      ...c,
      states: on ? [...c.states, state] : c.states.filter((s) => s !== state),
    }));

  return (
    <div className="overlay">
      <div className="dialog">
        <h2>Archive tasks</h2>
        <p className="muted small">
          Archived tasks leave the board and every total. Nothing is deleted, and an admin can
          unarchive them.
        </p>
        <fieldset>
          <legend>States</legend>
          {ARCHIVABLE.map((state) => (
            <label className="check" key={state}>
              <input
                type="checkbox"
                checked={criteria.states.includes(state)}
                onChange={(e) => toggle(state, e.target.checked)}
              />
              {state}
            </label>
          ))}
        </fieldset>
        <div className="row">
          <label>
            Project
            <select
              value={criteria.project}
              onChange={(e) => setCriteria((c) => ({ ...c, project: e.target.value }))}
            >
              <option value="">All projects</option>
              {(projects ?? []).map((p) => (
                <option key={p.slug} value={p.slug}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Last changed more than (days ago)
            <input
              type="number"
              min={0}
              placeholder="any time"
              value={criteria.olderThanDays}
              onChange={(e) => setCriteria((c) => ({ ...c, olderThanDays: e.target.value }))}
            />
          </label>
        </div>
        {error && <p className="error">{error}</p>}
        <div className="actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="primary" disabled={busy || !count} onClick={confirm}>
            {confirmLabel(count)}
          </button>
        </div>
      </div>
    </div>
  );
}
