import { useEffect, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { useLiveData } from "../live";
import { compactDraft, type Draft, emptyChains, toDraft } from "../models";
import { PageHeader } from "../PageHeader";
import {
  chainFromSelect,
  chainSelectValue,
  DEFAULT_DIFFICULTY_EXECUTORS,
  DIFFICULTY_EXECUTOR_PRESETS,
  DIFFICULTY_LEVELS,
  type DifficultyExecutors,
  type DifficultyLevel,
  fetchModelsOverride,
  fetchProjects,
  fetchSettings,
  type IndependentReview,
  putSetting,
  type ReviewMode,
  resetSetting,
  type SettingValue,
  sourceLabel,
} from "../settings";
import { ModelPreferences, ModelPreferencesEditor } from "./ModelPreferences";

/** Global settings with per-project overrides (docs/specs/settings.spec.md). */
export function Settings() {
  const { data: projects } = useLiveData((signal) => fetchProjects(signal), []);
  const [scope, setScope] = useState<string>("global");
  const project = scope === "global" ? null : scope;
  const { data, error, reload } = useLiveData((signal) => fetchSettings(project, signal), [scope]);

  const byKey = new Map((data?.settings ?? []).map((s) => [s.key, s]));
  const review = byKey.get("review.independent");
  const models = byKey.get("models");
  const difficultyExecutors = byKey.get("difficulty.executors");

  return (
    <section className="settings-page">
      <PageHeader
        title="Settings"
        lead="Operator choices with per-project overrides. An override replaces the global value for that project; Reset restores inheritance."
      >
        <label>
          Scope{" "}
          <select
            aria-label="settings scope"
            value={scope}
            onChange={(e) => setScope(e.target.value)}
          >
            <option value="global">Global</option>
            {(projects ?? [])
              .filter((p) => p.slug !== "global")
              .map((p) => (
                <option key={p.slug} value={p.slug}>
                  {p.name} ({p.slug})
                </option>
              ))}
          </select>
        </label>
        <button type="button" onClick={() => reload()}>
          Reload
        </button>
      </PageHeader>
      {error && <p className="error">{error.message}</p>}
      {!data && !error && <p className="muted">Loading…</p>}
      {data && (
        <>
          <div className="card">
            <ModelsSection project={project} setting={models} onChanged={() => reload()} />
          </div>
          <div className="card">
            <DifficultyExecutorsSection
              project={project}
              setting={difficultyExecutors}
              onChanged={() => reload()}
            />
          </div>
          <div className="card">
            <IndependentReviewSection
              project={project}
              setting={review}
              onChanged={() => reload()}
            />
          </div>
        </>
      )}
    </section>
  );
}

function ModelsSection(props: {
  project: string | null;
  setting?: SettingValue;
  onChanged: () => void;
}) {
  const { project, setting, onChanged } = props;
  if (project === null) {
    return (
      <>
        {setting?.notice && (
          <p className="error" role="alert">
            {setting.notice}
          </p>
        )}
        <ModelPreferences />
      </>
    );
  }
  return <ProjectModelsEditor project={project} setting={setting} onChanged={onChanged} />;
}

function ProjectModelsEditor(props: {
  project: string;
  setting?: SettingValue;
  onChanged: () => void;
}) {
  const { project, setting, onChanged } = props;
  const [draft, setDraft] = useState<Draft>({ harness: {}, implementation: {} });
  const [failure, setFailure] = useState<string>();
  const [done, setDone] = useState<string>();
  const action = useAction();
  const inherited = setting?.source !== "project";

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const override = await fetchModelsOverride(project);
        if (cancelled) return;
        if (override.value && typeof override.value === "object") {
          setDraft(toDraft(override.value as Parameters<typeof toDraft>[0]));
        } else {
          setDraft({ harness: {}, implementation: {} });
        }
      } catch (e) {
        if (!cancelled) setFailure(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [project, setting?.updated_at]);

  async function save() {
    await action.run(async () => {
      setFailure(undefined);
      setDone(undefined);
      try {
        await putSetting("models", compactDraft(draft), project);
        setDone("Saved project model override.");
        onChanged();
      } catch (e) {
        setFailure(e instanceof Error ? e.message : String(e));
      }
    }, "save");
  }

  async function reset() {
    await action.run(async () => {
      setFailure(undefined);
      setDone(undefined);
      try {
        await resetSetting("models", project);
        setDraft({ harness: {}, implementation: {} });
        setDone("Reset to inherited.");
        onChanged();
      } catch (e) {
        setFailure(e instanceof Error ? e.message : String(e));
      }
    }, "reset");
  }

  return (
    <section className="model-preferences">
      <div className="toolbar">
        <h3>Model preferences</h3>
      </div>
      <p className="muted small">
        Project override: only the chains you set here replace the global ones for this project.
        Leave a map empty to inherit it. Effective source:{" "}
        <strong>{setting ? sourceLabel(setting.source) : "…"}</strong>
        {setting?.revision && (
          <>
            {" "}
            · revision <code title={setting.revision}>{setting.revision.slice(0, 8)}</code>
          </>
        )}
      </p>
      <ModelPreferencesEditor draft={draft} onChange={setDraft} empties={emptyChains(draft)} />
      <div className="toolbar model-actions">
        <ActionButton
          type="button"
          disabled={action.busy}
          pending={action.is("save")}
          action={() => void save()}
        >
          Save override
        </ActionButton>
        <ActionButton
          type="button"
          disabled={action.busy || inherited}
          pending={action.is("reset")}
          action={() => void reset()}
        >
          Reset to inherited
        </ActionButton>
      </div>
      {failure && (
        <p className="error" role="alert">
          {failure}
        </p>
      )}
      {done && <p className="small">{done}</p>}
    </section>
  );
}

function DifficultyExecutorsSection(props: {
  project: string | null;
  setting?: SettingValue;
  onChanged: () => void;
}) {
  const { project, setting, onChanged } = props;
  const stored = (setting?.value ?? {}) as DifficultyExecutors;
  const [draft, setDraft] = useState<DifficultyExecutors>(stored);
  const action = useAction();
  const [failure, setFailure] = useState<string>();
  const [done, setDone] = useState<string>();
  const inherited =
    setting?.source === "default" ||
    (project !== null && setting?.source !== "project") ||
    (project === null && setting?.source !== "global" && setting?.source !== "env");

  useEffect(() => {
    setDraft((setting?.value ?? {}) as DifficultyExecutors);
  }, [setting?.updated_at, setting?.value]);

  function setLevel(level: DifficultyLevel, select: string) {
    const next = { ...draft };
    const chain = chainFromSelect(select);
    if (chain === undefined) delete next[level];
    else next[level] = chain;
    setDraft(next);
  }

  async function save() {
    await action.run(async () => {
      setFailure(undefined);
      setDone(undefined);
      try {
        await putSetting("difficulty.executors", draft, project);
        setDone("Saved.");
        onChanged();
      } catch (e) {
        setFailure(e instanceof Error ? e.message : String(e));
      }
    }, "save");
  }

  async function reset() {
    await action.run(async () => {
      setFailure(undefined);
      setDone(undefined);
      try {
        await resetSetting("difficulty.executors", project);
        setDraft({});
        setDone("Reset to inherited.");
        onChanged();
      } catch (e) {
        setFailure(e instanceof Error ? e.message : String(e));
      }
    }, "reset");
  }

  return (
    <section>
      <div className="toolbar">
        <h3>Difficulty executors</h3>
      </div>
      <p className="muted small">
        Ordered CLI chain per task difficulty. Empty / None means the harness does the work with no
        delegated executor (for example trivial → local model only). Unset levels keep the built-in
        default. Stored: <strong>{setting ? sourceLabel(setting.source) : "…"}</strong>
      </p>
      {setting?.notice && (
        <p className="error" role="alert">
          {setting.notice}
        </p>
      )}
      <div className="review-fields">
        {DIFFICULTY_LEVELS.map((level) => {
          const value = chainSelectValue(draft[level]);
          const fallback = DEFAULT_DIFFICULTY_EXECUTORS[level].join(" → ");
          return (
            <label key={level}>
              {level}
              <select
                aria-label={`difficulty executors ${level}`}
                value={value}
                disabled={setting?.source === "env"}
                onChange={(e) => setLevel(level, e.target.value)}
              >
                {DIFFICULTY_EXECUTOR_PRESETS.map((p) => (
                  <option key={p.value || "default"} value={p.value}>
                    {p.value === "" ? `Default (${fallback})` : p.label}
                  </option>
                ))}
              </select>
            </label>
          );
        })}
      </div>
      <div className="toolbar model-actions">
        <ActionButton
          type="button"
          disabled={action.busy || setting?.source === "env"}
          pending={action.is("save")}
          action={() => void save()}
        >
          Save
        </ActionButton>
        <ActionButton
          type="button"
          disabled={action.busy || inherited}
          pending={action.is("reset")}
          action={() => void reset()}
        >
          Reset to inherited
        </ActionButton>
      </div>
      {failure && (
        <p className="error" role="alert">
          {failure}
        </p>
      )}
      {done && <p className="small">{done}</p>}
    </section>
  );
}

function IndependentReviewSection(props: {
  project: string | null;
  setting?: SettingValue;
  onChanged: () => void;
}) {
  const { project, setting, onChanged } = props;
  const value = (setting?.value ?? { mode: "off", max_rounds: 3 }) as IndependentReview;
  const [mode, setMode] = useState<ReviewMode>(value.mode);
  const [rounds, setRounds] = useState(value.max_rounds);
  const action = useAction();
  const [failure, setFailure] = useState<string>();
  const [done, setDone] = useState<string>();
  const inherited =
    setting?.source === "default" ||
    (project !== null && setting?.source !== "project") ||
    (project === null && setting?.source !== "global");

  useEffect(() => {
    setMode(value.mode);
    setRounds(value.max_rounds);
  }, [value.mode, value.max_rounds, setting?.updated_at]);

  async function save() {
    await action.run(async () => {
      setFailure(undefined);
      setDone(undefined);
      try {
        await putSetting("review.independent", { mode, max_rounds: rounds }, project);
        setDone("Saved.");
        onChanged();
      } catch (e) {
        setFailure(e instanceof Error ? e.message : String(e));
      }
    }, "save");
  }

  async function reset() {
    await action.run(async () => {
      setFailure(undefined);
      setDone(undefined);
      try {
        await resetSetting("review.independent", project);
        setDone("Reset to inherited.");
        onChanged();
      } catch (e) {
        setFailure(e instanceof Error ? e.message : String(e));
      }
    }, "reset");
  }

  return (
    <section>
      <div className="toolbar">
        <h3>Independent code review</h3>
      </div>
      <p className="muted small">
        Require review by a different model before a code task succeeds. Stored setting:{" "}
        <strong>{setting ? sourceLabel(setting.source) : "…"}</strong>
      </p>
      <div className="review-fields">
        <label>
          Mode
          <select
            aria-label="independent review mode"
            value={mode}
            onChange={(e) => setMode(e.target.value as ReviewMode)}
          >
            <option value="off">Off</option>
            <option value="different_model">Require review by a different model</option>
            <option value="different_provider">
              Require review by a different model from a different provider
            </option>
          </select>
        </label>
        <label>
          Max rounds
          <input
            aria-label="independent review max rounds"
            type="number"
            min={1}
            max={20}
            value={rounds}
            onChange={(e) => setRounds(Number(e.target.value) || 1)}
          />
        </label>
      </div>
      <div className="toolbar model-actions">
        <ActionButton
          type="button"
          disabled={action.busy}
          pending={action.is("save")}
          action={() => void save()}
        >
          Save
        </ActionButton>
        <ActionButton
          type="button"
          disabled={action.busy || inherited}
          pending={action.is("reset")}
          action={() => void reset()}
        >
          Reset to inherited
        </ActionButton>
      </div>
      {failure && (
        <p className="error" role="alert">
          {failure}
        </p>
      )}
      {done && <p className="small">{done}</p>}
    </section>
  );
}
