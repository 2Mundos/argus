import { useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { ApiError, api, call, type Task } from "../api";
import { DELIVERABLES } from "../deliverables";
import {
  buildChanges,
  type Draft,
  EDIT_FIELDS,
  type EditField,
  errorsFrom,
  type FieldErrors,
  toDraft,
} from "../taskEdit";
import { AccountPin } from "./AccountPin";
import { ModelPin } from "./ModelPin";

const LABELS: Record<EditField, string> = {
  title: "Title",
  spec: "Spec",
  acceptance: "Acceptance criteria (one per line)",
  priority: "Priority",
  required_capabilities: "Required capabilities (one per line)",
  knowledge: "Pinned page ids (one per line)",
  depends_on: "Depends on (task ids, one per line)",
  difficulty: "Difficulty",
  deliverable: "Deliverable (what the task must leave behind)",
  account_providers: "Restrict to account providers (one per line, e.g. local; empty: any)",
  harness_model: "Harness engine and model (pinned for every attempt)",
  executor_model: "Coding executor and model (pinned for every attempt)",
  harness_account: "Harness account (e.g. strata-local or an OpenRouter account; empty: any)",
};

const MULTILINE = new Set<EditField>([
  "spec",
  "acceptance",
  "required_capabilities",
  "knowledge",
  "depends_on",
  "account_providers",
]);

/**
 * Edit form for one task. It works from `task` as it was when opened (or last re-based) and
 * never from live refreshes, so an operator's draft is never overwritten; a stale version
 * keeps the draft and lets the operator choose to reload or to re-base it.
 */
export function TaskEditForm({ task, onDone }: { task: Task; onDone: (saved?: Task) => void }) {
  const [base, setBase] = useState(task);
  const [draft, setDraft] = useState<Draft>(toDraft(task));
  const [errors, setErrors] = useState<FieldErrors>({});
  const [conflict, setConflict] = useState<string>();
  const action = useAction();
  const allowed = new Set<string>(base.editable_fields ?? []);

  async function latest(): Promise<Task> {
    return call(api.GET("/api/tasks/{task_id}", { params: { path: { task_id: base.id } } }));
  }

  async function save() {
    const { changes, errors: local } = buildChanges(base, draft);
    setConflict(undefined);
    if (Object.keys(local).length > 0) return setErrors(local);
    if (Object.keys(changes).length === 0) return onDone();
    await action.run(async () => {
      try {
        const saved = await call(
          api.PATCH("/api/tasks/{task_id}", {
            params: { path: { task_id: base.id } },
            body: { expected_version: base.version, changes },
          }),
        );
        onDone(saved);
      } catch (err) {
        if (err instanceof ApiError && err.code === "stale_version") {
          setErrors({});
          setConflict(err.details[0] ?? "");
        } else setErrors(errorsFrom(err));
      }
    });
  }

  async function reload() {
    const fresh = await latest();
    setBase(fresh);
    setDraft(toDraft(fresh));
    setConflict(undefined);
    setErrors({});
  }

  /** Re-base: the operator's own edits win; every other field takes the latest value. */
  async function keepEditing() {
    const fresh = await latest();
    const before = toDraft(base);
    const mine = Object.fromEntries(
      EDIT_FIELDS.filter((f) => draft[f] !== before[f]).map((f) => [f, draft[f]]),
    );
    setBase(fresh);
    setDraft({ ...toDraft(fresh), ...mine });
    setConflict(undefined);
  }

  return (
    <form
      className="edit-form"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      {conflict !== undefined && (
        <div className="error" role="alert">
          <p>
            Someone changed this task while you were editing (now at version {conflict}). Your draft
            is kept.
          </p>
          <div className="actions left">
            <button type="button" onClick={() => void reload()}>
              Reload latest
            </button>
            <button type="button" className="primary" onClick={() => void keepEditing()}>
              Keep editing on the latest version
            </button>
          </div>
        </div>
      )}
      {EDIT_FIELDS.map((field) => {
        const id = `edit-${field}`;
        const disabled = !allowed.has(field);
        const common = {
          id,
          disabled,
          value: draft[field],
          "aria-invalid": errors[field] ? true : undefined,
          "aria-describedby": errors[field] ? `${id}-error` : undefined,
          onChange: (e: { target: { value: string } }) =>
            setDraft({ ...draft, [field]: e.target.value }),
        };
        return (
          <div key={field} className="field">
            <label htmlFor={id}>{LABELS[field]}</label>
            {MULTILINE.has(field) ? (
              <textarea rows={field === "spec" ? 8 : 3} {...common} />
            ) : field === "difficulty" ? (
              <select {...common}>
                <option value="standard">standard</option>
                <option value="trivial">trivial (cheapest executor)</option>
                <option value="simple">simple (cheap executor)</option>
                <option value="hard">hard (Claude runs on Opus)</option>
              </select>
            ) : field === "deliverable" ? (
              <select {...common}>
                {DELIVERABLES.map((d) => (
                  <option key={d.value} value={d.value}>
                    {d.label}
                  </option>
                ))}
              </select>
            ) : field === "harness_model" || field === "executor_model" ? (
              <ModelPin
                field={field}
                id={id}
                value={draft[field]}
                disabled={disabled}
                onChange={(value) => setDraft({ ...draft, [field]: value })}
              />
            ) : field === "harness_account" ? (
              <AccountPin
                id={id}
                value={draft[field]}
                disabled={disabled}
                onChange={(value) => setDraft({ ...draft, [field]: value })}
              />
            ) : (
              <input type={field === "priority" ? "number" : "text"} {...common} />
            )}
            {disabled && (
              <p className="muted small">Not editable while the task is {base.state}.</p>
            )}
            {errors[field] && (
              <p id={`${id}-error`} className="error">
                {errors[field]}
              </p>
            )}
          </div>
        );
      })}
      {errors.form && <p className="error">{errors.form}</p>}
      <div className="actions left">
        <ActionButton type="submit" className="primary" pending={action.busy}>
          Save
        </ActionButton>
        <button type="button" disabled={action.busy} onClick={() => onDone()}>
          Cancel
        </button>
      </div>
    </form>
  );
}
