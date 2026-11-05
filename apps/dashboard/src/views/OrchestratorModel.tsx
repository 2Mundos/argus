import { useEffect, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { MODEL_ID } from "../models";
import { putSetting, resetSetting } from "../settings";

/** What `orchestrator.model` can name (internal-orchestrator spec 3.1). `required`:
 * a harness engine needs a model; local and custom default to the account's. */
const PROVIDERS: { value: string; label: string; required: boolean; placeholder: string }[] = [
  { value: "local", label: "Local model", required: false, placeholder: "the account's model" },
  { value: "custom", label: "Custom", required: false, placeholder: "the account's model" },
  {
    value: "claude",
    label: "Anthropic (claude)",
    required: true,
    placeholder: "claude-sonnet-5-5",
  },
  { value: "codex", label: "OpenAI (codex)", required: true, placeholder: "gpt-5.5-codex" },
];

/** `local/qwen3` → ["local", "qwen3"]; `local` → ["local", ""]; null → ["", ""]. */
export function splitPreference(value: unknown): [string, string] {
  if (typeof value !== "string" || value === "") return ["", ""];
  const at = value.indexOf("/");
  return at < 0 ? [value, ""] : [value.slice(0, at), value.slice(at + 1)];
}

/** The internal orchestrator's provider and model picker (builtin-orchestrator
 * spec 3.14), in Settings and onboarding step 3. */
export function OrchestratorModelPicker(props: { value: unknown; onChanged: () => void }) {
  const [storedProvider, storedModel] = splitPreference(props.value);
  const [provider, setProvider] = useState(storedProvider);
  const [model, setModel] = useState(storedModel);
  const action = useAction();
  const [failure, setFailure] = useState<string>();
  const [done, setDone] = useState<string>();

  useEffect(() => {
    setProvider(storedProvider);
    setModel(storedModel);
  }, [storedProvider, storedModel]);

  const chosen = PROVIDERS.find((p) => p.value === provider);
  const trimmed = model.trim();
  const invalid = chosen
    ? chosen.required
      ? !MODEL_ID.test(trimmed)
      : trimmed !== "" && !MODEL_ID.test(trimmed)
    : false;
  const unchanged = provider === storedProvider && trimmed === storedModel;

  async function save() {
    await action.run(async () => {
      setFailure(undefined);
      setDone(undefined);
      try {
        if (provider === "") await resetSetting("orchestrator.model", null);
        else
          await putSetting(
            "orchestrator.model",
            trimmed ? `${provider}/${trimmed}` : provider,
            null,
          );
        setDone(
          "Saved. It applies when the orchestrator next leases an account; Disable and Enable it to apply it now.",
        );
        props.onChanged();
      } catch (e) {
        setFailure(e instanceof Error ? e.message : String(e));
      }
    }, "save");
  }

  return (
    <section>
      <p className="muted small">
        Preferred provider and model. Automatic uses the best-ranked account. When the preferred
        provider has no usable account, the ordinary ranking applies.
      </p>
      <div className="toolbar">
        <label>
          Provider{" "}
          <select
            aria-label="orchestrator provider"
            value={provider}
            disabled={action.busy}
            onChange={(e) => setProvider(e.target.value)}
          >
            <option value="">Automatic</option>
            {PROVIDERS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
        </label>
        {chosen && (
          <label>
            Model{" "}
            <input
              aria-label="orchestrator model"
              value={model}
              placeholder={chosen.placeholder}
              disabled={action.busy}
              onChange={(e) => setModel(e.target.value)}
            />
          </label>
        )}
        <ActionButton
          type="button"
          disabled={action.busy || invalid || unchanged}
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
