import type { components } from "@argus/api-client";
import { useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { ApiError, api, call, type Task } from "../api";
import { DateChip } from "../DateChip";
import { useLiveData } from "../live";

const SELECTABLE = new Set<Task["state"]>(["awaiting_approval", "pending", "ready", "blocked"]);

function failure(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401 || error.status === 403)
      return "An admin token is required to choose integrations.";
    if (error.status === 409)
      return "The task or a connection changed, or an attempt is running. Refresh and try again.";
    if (error.status === 422) return "Choose active Kubernetes connections of this task's project.";
  }
  return "The integrations could not be saved. Try again.";
}

type Connection = components["schemas"]["Integration"];

/** The project's integrations, for a picker; admin only (null without an admin token). */
function useProjectKubernetes(project: string) {
  const { data } = useLiveData(
    (signal) =>
      project
        ? call(
            api.GET("/api/projects/{slug}/integrations", {
              params: { path: { slug: project } },
              signal,
            }),
          )
        : Promise.resolve(undefined),
    [project],
    { accepts: (event) => event.subject_type === "integration" },
  );
  return data;
}

/** Why a selected connection would block an attempt now; null when it is deliverable. */
function readiness(item: Connection): string | null {
  if (item.state !== "active") return item.state;
  if (item.expires_at && Date.parse(item.expires_at) <= Date.now()) return "expired";
  if (item.validation !== "ok") return "not validated: attempts block until it is";
  return null;
}

function Choices({
  items,
  chosen,
  onChange,
}: {
  items: Connection[];
  chosen: readonly string[];
  onChange: (ids: string[]) => void;
}) {
  return items.map((item) => {
    const problem = readiness(item);
    return (
      <label key={item.id}>
        <input
          type="checkbox"
          checked={chosen.includes(item.id)}
          onChange={(event) =>
            onChange(
              event.target.checked ? [...chosen, item.id] : chosen.filter((id) => id !== item.id),
            )
          }
        />{" "}
        {item.name} · namespace {String(item.config.namespace)} ·{" "}
        {item.access === "read_only" ? "read only" : "read and write"}
        {item.expires_at && !problem && (
          <>
            {" "}
            · expires <DateChip at={item.expires_at} />
          </>
        )}
        {problem && <span className="error small"> · {problem}</span>}
      </label>
    );
  });
}

const HELP =
  "Each attempt gets an ephemeral kubeconfig pinned to these namespaces, on Claude engines only.";

/**
 * Kubernetes connections of `project` for a new task (integrations spec 3.6). Renders
 * nothing when the project has none or the caller cannot list them (admin only).
 */
export function NewTaskIntegrations({
  project,
  chosen,
  onChange,
}: {
  project: string;
  chosen: readonly string[];
  onChange: (ids: string[]) => void;
}) {
  const items = useProjectKubernetes(project);
  const kubernetes = (items ?? []).filter(
    (item) => item.kind === "kubernetes" && item.state === "active",
  );
  if (kubernetes.length === 0) return null;
  return (
    <fieldset aria-label="Integrations">
      <legend>Kubernetes integrations (none: no cluster access)</legend>
      <Choices items={kubernetes} chosen={chosen} onChange={onChange} />
      <p className="muted small">{HELP}</p>
    </fieldset>
  );
}

/**
 * The task's opt-in to Kubernetes connections of its project (integrations spec 3.6).
 * Each attempt gets an ephemeral kubeconfig for the selected connections, Claude engines only.
 */
export function TaskIntegrations({ task, onSaved }: { task: Task; onSaved: () => void }) {
  const items = useProjectKubernetes(task.project);
  const selected = task.integrations ?? [];
  const [draft, setDraft] = useState<string[]>();
  const [error, setError] = useState<string>();
  const action = useAction();
  const kubernetes = (items ?? []).filter(
    (item) => item.kind === "kubernetes" && (item.state === "active" || selected.includes(item.id)),
  );
  if (kubernetes.length === 0 && selected.length === 0) return null;
  const editable = SELECTABLE.has(task.state) && !task.archived_at;
  const chosen = draft ?? selected;

  async function save() {
    await action.run(async () => {
      setError(undefined);
      try {
        await call(
          api.PUT("/api/tasks/{task_id}/integrations", {
            params: { path: { task_id: task.id } },
            body: { expected_version: task.version, integrations: chosen },
          }),
        );
        setDraft(undefined);
        onSaved();
      } catch (error) {
        setError(failure(error));
      }
    });
  }

  return (
    <section aria-label="Integrations">
      <header className="task-section-label">Integrations</header>
      <fieldset disabled={!editable || action.busy}>
        <Choices items={kubernetes} chosen={chosen} onChange={setDraft} />
        {selected
          .filter((id) => items && !items.some((item) => item.id === id))
          .map((id) => (
            <div key={id} className="muted small">
              {id} (no longer listed)
            </div>
          ))}
      </fieldset>
      <p className="muted small">
        {HELP}
        {editable
          ? ""
          : " The selection cannot change while an attempt runs or after the task ends."}
      </p>
      {draft && (
        <div className="actions left">
          <ActionButton pending={action.busy} disabled={action.busy} action={save}>
            Save integrations
          </ActionButton>
          <button type="button" disabled={action.busy} onClick={() => setDraft(undefined)}>
            Cancel
          </button>
        </div>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
