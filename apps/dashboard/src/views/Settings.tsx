import { useEffect, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { ApiError } from "../api";
import { useLiveData } from "../live";
import { compactDraft, type Draft, emptyChains, MODEL_ID, toDraft } from "../models";
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
import { GitIdentityEditor } from "./GitIdentityEditor";
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
  const gitIdentity = byKey.get("git.identity");
  const orchestratorEnabled = byKey.get("orchestrator.enabled");
  const orchestratorRuntime = byKey.get("orchestrator.runtime");
  const orchestratorModel = byKey.get("orchestrator.model");

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
          {project === null && (
            <div className="card">
              <OrchestratorSection
                enabled={orchestratorEnabled?.value === true}
                runtime={(orchestratorRuntime?.value as Runtime | undefined) ?? "auto"}
                onChanged={() => reload()}
              />
              <OrchestratorModelSection setting={orchestratorModel} onChanged={() => reload()} />
            </div>
          )}
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
            <h3>Git author identity</h3>
            <p className="muted small">
              Author and committer for worker commits. A project override replaces the global value
              for that project's tasks; workers refuse to commit while neither is set.
            </p>
            <GitIdentityEditor
              key={scope}
              project={project}
              setting={gitIdentity}
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

type Runtime = "auto" | "builtin" | "nanoclaw";
const RUNTIMES: { value: Runtime; label: string }[] = [
  { value: "auto", label: "Automatic" },
  { value: "builtin", label: "Built-in (inside Argus)" },
  { value: "nanoclaw", label: "NanoClaw (container)" },
];

/** The internal orchestrator's switch and runtime (builtin-orchestrator spec 3.14). */
function OrchestratorSection(props: { enabled: boolean; runtime: Runtime; onChanged: () => void }) {
  const { enabled, runtime, onChanged } = props;
  const action = useAction();
  const [failure, setFailure] = useState<{ message: string; running: boolean }>();

  async function write(key: string, value: unknown, pendingKey: string) {
    await action.run(async () => {
      setFailure(undefined);
      try {
        await putSetting(key, value, null);
        onChanged();
      } catch (e) {
        const running = e instanceof ApiError && e.code === "orchestrator_running";
        const details =
          e instanceof ApiError && e.details.length ? `: ${e.details.join("; ")}` : "";
        setFailure({ message: `${e instanceof Error ? e.message : String(e)}${details}`, running });
      }
    }, pendingKey);
  }

  return (
    <section>
      <div className="toolbar">
        <h3>Internal orchestrator</h3>
      </div>
      <p className="muted small">
        {enabled ? "Enabled." : "Disabled."} The runtime applies when it is enabled: Built-in runs
        inside Argus on an API-key, custom or local account; NanoClaw runs a container. Automatic
        picks Built-in when such an account exists. Changing it needs a stopped orchestrator.
      </p>
      <div className="toolbar">
        <label>
          Runtime{" "}
          <select
            aria-label="orchestrator runtime"
            value={runtime}
            disabled={action.busy}
            onChange={(e) => void write("orchestrator.runtime", e.target.value, "runtime")}
          >
            {RUNTIMES.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
        </label>
        <ActionButton
          pending={action.is("enabled")}
          disabled={action.busy}
          action={() => write("orchestrator.enabled", !enabled, "enabled")}
        >
          {enabled ? "Disable" : "Enable"}
        </ActionButton>
      </div>
      {failure && (
        <p className="error" role="alert">
          {failure.message}
          {failure.running && enabled && (
            <>
              {" "}
              <button
                type="button"
                className="link"
                onClick={() => void write("orchestrator.enabled", false, "enabled")}
              >
                Disable
              </button>
            </>
          )}
        </p>
      )}
    </section>
  );
}

/** Harness engines `orchestrator.model` can name, with the account provider each
 * one leases (internal-orchestrator spec 3.1). */
const ORCHESTRATOR_ENGINES: { engine: string; label: string }[] = [
  { engine: "claude", label: "Anthropic (claude)" },
  { engine: "codex", label: "OpenAI (codex)" },
];

/** `orchestrator.model`: preferred provider and model (builtin-orchestrator spec 3.14). */
function OrchestratorModelSection(props: { setting?: SettingValue; onChanged: () => void }) {
  const { setting, onChanged } = props;
  const stored = typeof setting?.value === "string" ? setting.value : "";
  const [storedEngine, storedModel] = stored.includes("/")
    ? [stored.slice(0, stored.indexOf("/")), stored.slice(stored.indexOf("/") + 1)]
    : ["", ""];
  const [engine, setEngine] = useState(storedEngine);
  const [model, setModel] = useState(storedModel);
  const action = useAction();
  const [failure, setFailure] = useState<string>();
  const [done, setDone] = useState<string>();

  useEffect(() => {
    setEngine(storedEngine);
    setModel(storedModel);
  }, [storedEngine, storedModel]);

  async function save() {
    await action.run(async () => {
      setFailure(undefined);
      setDone(undefined);
      try {
        if (engine === "") await resetSetting("orchestrator.model", null);
        else await putSetting("orchestrator.model", `${engine}/${model.trim()}`, null);
        setDone(
          "Saved. It applies when the orchestrator next leases an account; Disable and Enable it to apply it now.",
        );
        onChanged();
      } catch (e) {
        setFailure(e instanceof Error ? e.message : String(e));
      }
    }, "save");
  }

  const invalid = engine !== "" && !MODEL_ID.test(model.trim());
  return (
    <section>
      <p className="muted small">
        Preferred provider and model. Automatic uses the best-ranked account and the model policy's
        harness chain. When the preferred provider has no usable account, the ordinary ranking
        applies. Custom and local accounts use the model set on the account.
      </p>
      <div className="toolbar">
        <label>
          Provider{" "}
          <select
            aria-label="orchestrator provider"
            value={engine}
            disabled={action.busy}
            onChange={(e) => setEngine(e.target.value)}
          >
            <option value="">Automatic</option>
            {ORCHESTRATOR_ENGINES.map((o) => (
              <option key={o.engine} value={o.engine}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        {engine !== "" && (
          <label>
            Model{" "}
            <input
              aria-label="orchestrator model"
              value={model}
              placeholder={engine === "claude" ? "claude-sonnet-5-5" : "gpt-5.5-codex"}
              disabled={action.busy}
              onChange={(e) => setModel(e.target.value)}
            />
          </label>
        )}
        <ActionButton
          type="button"
          disabled={action.busy || invalid || (engine === storedEngine && model === storedModel)}
          pending={action.is("save")}
          action={() => void save()}
        >
          Save
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
