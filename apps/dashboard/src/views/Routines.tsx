import { useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { ApiError, api, call, type TaskState } from "../api";
import { DateChip } from "../DateChip";
import { useLiveData } from "../live";
import { PageHeader } from "../PageHeader";

type Routine = Awaited<ReturnType<typeof loadRoutines>>[number];

/** The list route leaves `last_runs` empty, so read each routine for its run states. */
async function loadRoutines(project: string, signal: AbortSignal) {
  const list = await call(
    api.GET("/api/projects/{slug}/routines", { params: { path: { slug: project } }, signal }),
  );
  return Promise.all(
    list.map(async (routine) => {
      try {
        return await call(
          api.GET("/api/routines/{routine_id}", {
            params: { path: { routine_id: routine.id } },
            signal,
          }),
        );
      } catch (err) {
        if (signal.aborted) throw err;
        // Keep the listed routine; its last run's state is then shown as unknown.
        return { ...routine, last_runs: null };
      }
    }),
  );
}

/** A time in the routine's own timezone; falls back to UTC if the name is not one the browser knows. */
export function inZone(iso: string, timezone: string): string {
  const options: Intl.DateTimeFormatOptions = {
    dateStyle: "medium",
    timeStyle: "short",
    hour12: false,
  };
  try {
    return `${new Intl.DateTimeFormat("en-GB", { ...options, timeZone: timezone }).format(new Date(iso))} ${timezone}`;
  } catch {
    return `${new Intl.DateTimeFormat("en-GB", { ...options, timeZone: "UTC" }).format(new Date(iso))} UTC`;
  }
}

function When({ at, timezone }: { at: string; timezone: string }) {
  return (
    <>
      <DateChip at={at} /> <span className="muted small">{inZone(at, timezone)}</span>
    </>
  );
}

/** What the API said, with the status for the auth failures the caller can act on. */
function failure(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 401 || err.status === 403) {
      return `Not permitted (${err.status}): ${err.message}`;
    }
    return err.message;
  }
  return err instanceof Error ? err.message : "The request failed";
}

type Notice = { kind: "error" | "skipped" | "started"; text: string };

export function Routines() {
  const [project, setProject] = useState("");
  const { data: projects, error: projectsError } = useLiveData(
    (signal) => call(api.GET("/api/projects", { signal })),
    [],
  );
  const slug = project || projects?.[0]?.slug || "";
  const {
    data: routines,
    error,
    reload,
  } = useLiveData(
    (signal) => (slug ? loadRoutines(slug, signal) : Promise.resolve(undefined)),
    [slug],
  );

  return (
    <section className="routines">
      <PageHeader
        title="Routines"
        lead="Recurring work on a schedule. Each run is an ordinary task."
      >
        <select aria-label="Project" value={slug} onChange={(e) => setProject(e.target.value)}>
          {(projects ?? []).map((p) => (
            <option key={p.slug} value={p.slug}>
              {p.name}
            </option>
          ))}
        </select>
      </PageHeader>
      {projectsError ? (
        <p className="error" role="alert">
          Could not load projects: {projectsError.message}
        </p>
      ) : !projects ? (
        <p className="muted">Loading…</p>
      ) : !slug ? (
        <p className="muted">No projects yet.</p>
      ) : error ? (
        <div role="alert">
          <p className="error">Could not load routines: {error.message}</p>
          <button type="button" onClick={reload}>
            Retry
          </button>
        </div>
      ) : !routines ? (
        <p className="muted">Loading…</p>
      ) : routines.length === 0 ? (
        <p className="muted">No routines in this project.</p>
      ) : (
        <ul className="routine-list">
          {routines.map((routine) => (
            <RoutineCard key={routine.id} routine={routine} onChanged={reload} />
          ))}
        </ul>
      )}
    </section>
  );
}

function RoutineCard({ routine, onChanged }: { routine: Routine; onChanged: () => void }) {
  const action = useAction();
  const [notice, setNotice] = useState<Notice | null>(null);
  const tz = routine.timezone;

  async function act(kind: "toggle" | "run") {
    await action.run(async () => {
      setNotice(null);
      try {
        if (kind === "toggle") {
          await call(
            api.PATCH("/api/projects/{slug}/routines/{routine}", {
              params: { path: { slug: routine.project, routine: routine.slug } },
              body: { enabled: !routine.enabled },
            }),
          );
        } else {
          const result = await call(
            api.POST("/api/routines/{routine_id}/run", {
              params: { path: { routine_id: routine.id } },
            }),
          );
          setNotice(
            result.skipped === "previous_running"
              ? {
                  kind: "skipped",
                  text: "Already running, not started: the previous run is still open.",
                }
              : { kind: "started", text: `Run started as task ${result.task_id ?? "unknown"}.` },
          );
        }
      } catch (err) {
        setNotice({ kind: "error", text: failure(err) });
      } finally {
        onChanged();
      }
    }, kind);
  }

  return (
    <li className="routine-card">
      <div className="routine-head">
        <h3>{routine.title}</h3>
        <span className="muted small">{routine.slug}</span>
        <span className={`badge ${routine.enabled ? "state-succeeded" : "state-blocked"}`}>
          {routine.enabled ? "enabled" : "disabled"}
        </span>
      </div>
      <dl className="routine-facts">
        <dt>Schedule</dt>
        <dd>
          <code>{routine.schedule}</code> <span className="muted small">{tz}</span>
        </dd>
        <dt>Next run</dt>
        <dd>
          {routine.next_run_at ? (
            <When at={routine.next_run_at} timezone={tz} />
          ) : (
            <span className="muted">{routine.enabled ? "unknown" : "none while disabled"}</span>
          )}
          {routine.next_slots.length > 0 && (
            <div className="muted small">
              Next slots: {routine.next_slots.map((s) => inZone(s, tz)).join(" · ")}
            </div>
          )}
        </dd>
        <dt>Last run</dt>
        <dd>
          <LastRun routine={routine} />
        </dd>
        <dt>Findings</dt>
        <dd>
          <FindingsFiled routine={routine} />{" "}
          <span className="muted small">mode: {routine.findings}</span>
        </dd>
      </dl>
      <div className="routine-actions">
        <ActionButton
          type="button"
          disabled={action.busy}
          pending={action.is("toggle")}
          pendingLabel={routine.enabled ? "Disabling…" : "Enabling…"}
          action={() => void act("toggle")}
        >
          {routine.enabled ? "Disable" : "Enable"}
        </ActionButton>
        <ActionButton
          type="button"
          disabled={action.busy}
          pending={action.is("run")}
          pendingLabel="Starting…"
          action={() => void act("run")}
        >
          Run now
        </ActionButton>
      </div>
      {notice && (
        <p
          className={notice.kind === "error" ? "error" : "muted small"}
          role={notice.kind === "error" ? "alert" : "status"}
        >
          {notice.text}
        </p>
      )}
    </li>
  );
}

/** The last run's filed count, read from the detail; unknown is never shown as 0. */
function FindingsFiled({ routine }: { routine: Routine }) {
  if (!routine.last_run_at && !routine.last_task_id) {
    return <span className="muted">no runs yet</span>;
  }
  const filed = routine.last_runs?.find((r) => r.task_id === routine.last_task_id)?.findings_filed;
  if (filed === undefined) {
    return (
      <span className="muted" title="The last run's count is not in the routine detail">
        unknown (not reported)
      </span>
    );
  }
  return <span title="Findings filed by the last run">{filed} filed</span>;
}

function LastRun({ routine }: { routine: Routine }) {
  if (!routine.last_run_at && !routine.last_task_id) {
    // The scheduler sets both when it creates a run, so null means none has started.
    return <span className="muted">not run yet</span>;
  }
  const state: TaskState | null | undefined = routine.last_runs
    ? routine.last_runs.find((r) => r.task_id === routine.last_task_id)?.state
    : undefined;
  return (
    <>
      {routine.last_run_at ? (
        <When at={routine.last_run_at} timezone={routine.timezone} />
      ) : (
        <span className="muted">time unknown</span>
      )}{" "}
      {routine.last_task_id ? (
        <a href={`#tasks/${routine.last_task_id}`}>task {routine.last_task_id.slice(0, 8)}</a>
      ) : (
        <span className="muted">task unknown</span>
      )}{" "}
      {state ? (
        <span className={`badge state-${state}`}>{state.replace("_", " ")}</span>
      ) : (
        <span className="muted small">state unknown</span>
      )}
    </>
  );
}
