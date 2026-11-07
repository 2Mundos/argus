import { useEffect, useId, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { ApiError } from "../api";
import { DELIVERABLES } from "../deliverables";
import { useLiveData } from "../live";
import { DIFFICULTY_LEVELS, fetchSettings, putSetting, resetSetting } from "../settings";
import {
  BUILT_IN,
  EMPTY_MEANS,
  FIELD_LABELS,
  fetchTaskDefaults,
  formatValue,
  parseText,
  sameValue,
  settingKey,
  sourceText,
  TASK_DEFAULT_FIELDS,
  type TaskDefaultField,
  toText,
} from "../taskDefaults";
import { useUnsavedChanges } from "../unsaved";

type FieldDraft = { override: boolean; text: string };

function message(e: unknown): string {
  if (e instanceof ApiError && e.details.length) return `${e.message}: ${e.details.join("; ")}`;
  return e instanceof Error ? e.message : String(e);
}

/**
 * Task defaults for one scope (task-defaults.spec.md 3.7): per field, inherit or
 * override, the effective value with its source, and the problems a create would hit.
 * `project` null edits the global values, whose fallback is the built-in one.
 */
export function TaskDefaultsEditor({ project }: { project: string | null }) {
  const id = useId();
  const own = project === null ? "global" : "project";
  const {
    data: settings,
    error: settingsError,
    reload: reloadSettings,
  } = useLiveData((signal) => fetchSettings(project, signal), [project]);
  // What a project field inherits: the global value, else the built-in one.
  const { data: global, reload: reloadGlobal } = useLiveData(
    (signal) => (project === null ? Promise.resolve(null) : fetchSettings(null, signal)),
    [project],
  );
  const {
    data: effective,
    error: effectiveError,
    reload: reloadEffective,
  } = useLiveData((signal) => fetchTaskDefaults(project ?? "global", "generic", signal), [project]);
  const [edits, setEdits] = useState<Partial<Record<TaskDefaultField, FieldDraft>>>({});
  const [failures, setFailures] = useState<Partial<Record<TaskDefaultField, string>>>({});
  const [done, setDone] = useState<string>();
  const action = useAction();

  const setting = (field: TaskDefaultField) =>
    settings?.settings.find((s) => s.key === settingKey(field));
  const overridden = (field: TaskDefaultField) => setting(field)?.source === own;
  const inherited = (field: TaskDefaultField): { value: unknown; from: string } => {
    const g = global?.settings.find((s) => s.key === settingKey(field));
    return g?.source === "global"
      ? { value: g.value, from: "global" }
      : { value: BUILT_IN[field], from: "built-in" };
  };
  const stored = (field: TaskDefaultField): FieldDraft => ({
    override: overridden(field),
    text: toText(field, overridden(field) ? setting(field)?.value : inherited(field).value),
  });
  const draft = (field: TaskDefaultField) => edits[field] ?? stored(field);
  const parsed = (field: TaskDefaultField) => parseText(field, draft(field).text);
  const isDirty = (field: TaskDefaultField, edit = edits[field]) => {
    if (!edit) return false;
    if (edit.override !== overridden(field)) return true;
    if (!edit.override) return false;
    const p = parseText(field, edit.text);
    return !("value" in p) || !sameValue(p.value, setting(field)?.value);
  };
  const dirtyFields = TASK_DEFAULT_FIELDS.filter((f) => isDirty(f));
  const invalid = TASK_DEFAULT_FIELDS.filter((f) => draft(f).override && "error" in parsed(f));
  useUnsavedChanges(dirtyFields.length > 0);

  // Once the saved values are read back, a field that matches them is no longer an edit.
  useEffect(() => {
    setEdits((prev) => {
      const next = { ...prev };
      for (const f of TASK_DEFAULT_FIELDS) if (!isDirty(f, prev[f])) delete next[f];
      return next;
    });
  }, [settings]);

  function edit(field: TaskDefaultField, change: Partial<FieldDraft>) {
    setDone(undefined);
    setEdits((prev) => ({ ...prev, [field]: { ...draft(field), ...change } }));
  }

  async function save() {
    await action.run(async () => {
      const failed: Partial<Record<TaskDefaultField, string>> = {};
      for (const field of dirtyFields) {
        const d = draft(field);
        const p = parsed(field);
        try {
          if (!d.override) await resetSetting(settingKey(field), project);
          else if ("value" in p) await putSetting(settingKey(field), p.value, project);
        } catch (e) {
          failed[field] = message(e);
        }
      }
      setFailures(failed);
      const count = Object.keys(failed).length;
      setDone(count ? undefined : "Task defaults saved.");
      reloadSettings();
      reloadGlobal();
      reloadEffective();
    }, "save");
  }

  if (settingsError && !settings) {
    return (
      <p className="error" role="alert">
        Could not load the task defaults: {settingsError.message}{" "}
        <button type="button" className="link" onClick={reloadSettings}>
          Retry
        </button>
      </p>
    );
  }
  if (!settings || (project !== null && global === undefined)) {
    return <p className="muted">Loading task defaults…</p>;
  }

  const problems = (effective?.problems ?? []).filter(
    // The global project has no repository; a repository problem is per project.
    (p) => project !== null || p.code !== "deliverable_repository",
  );
  const failedCount = Object.keys(failures).length;
  const inheritLabel = project === null ? "Built-in" : "Inherit";

  return (
    <section className="task-defaults" aria-label="Task defaults">
      <p className="muted small">
        {project === null
          ? "Values for the fields a new task leaves out, in every project that does not override them. Built-in restores the fallback."
          : "What a new task in this project gets for each field it leaves out. Inherit takes the global value; an override replaces it for this project only."}{" "}
        A value the creator sends always wins. Saving needs an admin token.
      </p>
      {project !== null && !TASK_DEFAULT_FIELDS.some(overridden) && dirtyFields.length === 0 && (
        <p className="muted small">No overrides: every field inherits.</p>
      )}
      {effectiveError && (
        <p className="error small" role="alert">
          Could not load the effective defaults: {effectiveError.message}{" "}
          <button type="button" className="link" onClick={reloadEffective}>
            Retry
          </button>
        </p>
      )}
      <div className="task-default-fields">
        {TASK_DEFAULT_FIELDS.map((field) => {
          const d = draft(field);
          const p = parsed(field);
          const error = d.override && "error" in p ? p.error : undefined;
          const problem = problems.find((x) => x.field === field);
          const base = `${id}-${field}`;
          const describedBy = [
            error && `${base}-error`,
            problem && `${base}-problem`,
            `${base}-effective`,
          ]
            .filter(Boolean)
            .join(" ");
          const from =
            project === null ? { value: BUILT_IN[field], from: "built-in" } : inherited(field);
          return (
            <fieldset key={field} className={`task-default${isDirty(field) ? " is-dirty" : ""}`}>
              <legend>
                {FIELD_LABELS[field]}
                {isDirty(field) && <span className="muted small"> · unsaved</span>}
              </legend>
              <div className="toolbar">
                <label className="check">
                  <input
                    type="radio"
                    name={base}
                    checked={!d.override}
                    disabled={action.busy}
                    onChange={() => edit(field, { override: false })}
                  />
                  {inheritLabel} ({from.from}: {formatValue(field, from.value)})
                </label>
                <label className="check">
                  <input
                    type="radio"
                    name={base}
                    checked={d.override}
                    disabled={action.busy}
                    onChange={() =>
                      edit(field, {
                        override: true,
                        text: edits[field] ? d.text : toText(field, from.value),
                      })
                    }
                  />
                  {project === null ? "Set" : "Override"}
                </label>
              </div>
              {d.override && (
                <FieldInput
                  field={field}
                  text={d.text}
                  disabled={action.busy}
                  invalid={Boolean(error)}
                  describedBy={describedBy}
                  onChange={(text) => edit(field, { text })}
                />
              )}
              {error && (
                <p className="error small" id={`${base}-error`}>
                  {error}
                </p>
              )}
              <p className="small" id={`${base}-effective`}>
                Effective now:{" "}
                {effective ? (
                  <>
                    <strong>{formatValue(field, effective.values[field])}</strong>{" "}
                    <span className="badge">{sourceText(effective.sources[field])}</span>
                  </>
                ) : (
                  <span className="muted">…</span>
                )}
              </p>
              {problem && (
                <p className="error small" id={`${base}-problem`}>
                  A task created with the saved defaults is refused: {problem.message}
                </p>
              )}
              {failures[field] && (
                <p className="error small" role="alert">
                  Not saved: {failures[field]}
                </p>
              )}
            </fieldset>
          );
        })}
      </div>
      <div className="toolbar model-actions">
        <ActionButton
          type="button"
          className="primary"
          disabled={action.busy || dirtyFields.length === 0 || invalid.length > 0}
          pending={action.is("save")}
          action={() => void save()}
        >
          Save task defaults
        </ActionButton>
        <button
          type="button"
          disabled={action.busy || Object.keys(edits).length === 0}
          onClick={() => {
            setEdits({});
            setFailures({});
            setDone(undefined);
          }}
        >
          Discard changes
        </button>
        {invalid.length > 0 && (
          <span className="error small">Fix the highlighted fields to save.</span>
        )}
      </div>
      {failedCount > 0 && (
        <p className="error" role="alert">
          {failedCount === 1 ? "One field was" : `${failedCount} fields were`} not saved; the others
          were.
        </p>
      )}
      {done && (
        <p className="small" role="status">
          {done}
        </p>
      )}
    </section>
  );
}

function FieldInput(props: {
  field: TaskDefaultField;
  text: string;
  disabled: boolean;
  invalid: boolean;
  describedBy: string;
  onChange: (text: string) => void;
}) {
  const { field, text, disabled, invalid, describedBy, onChange } = props;
  const common = {
    "aria-label": `${FIELD_LABELS[field]} value`,
    "aria-invalid": invalid || undefined,
    "aria-describedby": describedBy,
    disabled,
    value: text,
  };
  if (field === "deliverable") {
    return (
      <select {...common} onChange={(e) => onChange(e.target.value)}>
        {DELIVERABLES.map((d) => (
          <option key={d.value} value={d.value}>
            {d.label}
          </option>
        ))}
      </select>
    );
  }
  if (field === "difficulty") {
    return (
      <select {...common} onChange={(e) => onChange(e.target.value)}>
        <option value="">Infer from the title and spec</option>
        {DIFFICULTY_LEVELS.map((level) => (
          <option key={level} value={level}>
            {level}
          </option>
        ))}
      </select>
    );
  }
  const numeric = field !== "required_capabilities";
  return (
    <input
      {...common}
      type={numeric ? "number" : "text"}
      step={field === "budget_usd" ? "any" : numeric ? 1 : undefined}
      placeholder={EMPTY_MEANS[field] ? `empty: ${EMPTY_MEANS[field]}` : undefined}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}
