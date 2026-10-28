import { useEffect, useState } from "react";
import { useLiveData } from "../live";
import { compactDraft, type Draft, emptyChains, toDraft } from "../models";
import {
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

  return (
    <section>
      <div className="toolbar">
        <h2>Settings</h2>
        <button type="button" onClick={() => reload()}>
          Reload
        </button>
      </div>
      <p className="muted small">
        Operator choices with per-project overrides. An override replaces the global value for that
        project; Reset restores inheritance.
      </p>
      <label className="small">
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
      {error && <p className="error">{error.message}</p>}
      {!data && !error && <p className="muted">Loading…</p>}
      {data && (
        <>
          <ModelsSection project={project} setting={models} onChanged={() => reload()} />
          <IndependentReviewSection project={project} setting={review} onChanged={() => reload()} />
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
      <section>
        <p className="small">
          Source: <strong>{setting ? sourceLabel(setting.source) : "…"}</strong>
          {setting?.revision && (
            <>
              {" "}
              · revision <code title={setting.revision}>{setting.revision.slice(0, 8)}</code>
            </>
          )}
        </p>
        {setting?.notice && (
          <p className="error" role="alert">
            {setting.notice}
          </p>
        )}
        <ModelPreferences />
      </section>
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
  const [busy, setBusy] = useState(false);
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
    setBusy(true);
    setFailure(undefined);
    setDone(undefined);
    try {
      await putSetting("models", compactDraft(draft), project);
      setDone("Saved project model override.");
      onChanged();
    } catch (e) {
      setFailure(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setBusy(true);
    setFailure(undefined);
    setDone(undefined);
    try {
      await resetSetting("models", project);
      setDraft({ harness: {}, implementation: {} });
      setDone("Reset to inherited.");
      onChanged();
    } catch (e) {
      setFailure(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
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
      <div className="toolbar">
        <button type="button" disabled={busy} onClick={() => void save()}>
          Save override
        </button>
        <button type="button" disabled={busy || inherited} onClick={() => void reset()}>
          Reset to inherited
        </button>
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
  const [busy, setBusy] = useState(false);
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
    setBusy(true);
    setFailure(undefined);
    setDone(undefined);
    try {
      await putSetting("review.independent", { mode, max_rounds: rounds }, project);
      setDone("Saved.");
      onChanged();
    } catch (e) {
      setFailure(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setBusy(true);
    setFailure(undefined);
    setDone(undefined);
    try {
      await resetSetting("review.independent", project);
      setDone("Reset to inherited.");
      onChanged();
    } catch (e) {
      setFailure(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <div className="toolbar">
        <h3>Independent code review</h3>
      </div>
      <p className="muted small">
        Require review by a different model before a code task succeeds. Source:{" "}
        <strong>{setting ? sourceLabel(setting.source) : "…"}</strong>
      </p>
      <label className="small">
        Mode{" "}
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
      </label>{" "}
      <label className="small">
        Max rounds{" "}
        <input
          aria-label="independent review max rounds"
          type="number"
          min={1}
          max={20}
          value={rounds}
          onChange={(e) => setRounds(Number(e.target.value) || 1)}
        />
      </label>
      <div className="toolbar">
        <button type="button" disabled={busy} onClick={() => void save()}>
          Save
        </button>
        <button type="button" disabled={busy || inherited} onClick={() => void reset()}>
          Reset to inherited
        </button>
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
