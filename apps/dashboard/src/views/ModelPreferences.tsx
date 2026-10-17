import { useEffect, useState } from "react";
import { useLiveData } from "../live";
import {
  addProblem,
  type Draft,
  fetchModelPolicy,
  KNOWN_ENGINES,
  type ModelPolicy,
  moved,
  restoreModelDefaults,
  SCOPES,
  type Scope,
  saveModelPolicy,
  sourceLabel,
  toDraft,
  withChain,
} from "../models";

const TITLES: Record<Scope, string> = {
  harness: "Harness (NanoClaw)",
  implementation: "Implementation (argus-delegate)",
};

/** Ordered model chains per engine, from GET/PUT/DELETE /api/models. */
export function ModelPreferences() {
  const { data, error, reload } = useLiveData(() => fetchModelPolicy(), []);
  const [policy, setPolicy] = useState<ModelPolicy>();
  const [draft, setDraft] = useState<Draft>();
  const [failure, setFailure] = useState<string>();
  const [done, setDone] = useState<string>();
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (data) adopt(data);
  }, [data]);

  function adopt(p: ModelPolicy) {
    setPolicy(p);
    setDraft(toDraft(p));
  }

  async function run(action: () => Promise<ModelPolicy>, verb: string) {
    setBusy(true);
    setFailure(undefined);
    setDone(undefined);
    try {
      const next = await action();
      adopt(next);
      setDone(
        `${verb}: revision ${next.revision.slice(0, 8)}, source ${sourceLabel(next.source)}.${
          next.env_overrides
            ? " ARGUS_MODEL_POLICY still wins, so nothing changed in effect."
            : " Applies to instances started from now on."
        }`,
      );
    } catch (e) {
      setFailure(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const dirty = !!policy && !!draft && JSON.stringify(draft) !== JSON.stringify(toDraft(policy));

  return (
    <section className="model-preferences">
      <div className="toolbar">
        <h3>Model preferences</h3>
        <button type="button" onClick={() => reload()}>
          Reload
        </button>
      </div>
      <p className="muted small">
        Requested model IDs in order of preference; a later one is tried after a failure or a
        rejected review. Availability at the provider is not verified, so use IDs your CLI accepts.
        Changes apply to new instances.
      </p>
      {error && <p className="error">{error.message}</p>}
      {!policy && !error && <p className="muted">Loading…</p>}
      {policy && draft && (
        <>
          <p className="small">
            Source: <strong>{sourceLabel(policy.source)}</strong> · revision{" "}
            <code title={policy.revision}>{policy.revision.slice(0, 8)}</code>
            {policy.source === "defaults" && " · repository defaults, no file saved"}
            {policy.source === "file" && <span className="muted"> · {policy.path}</span>}
          </p>
          <p className="small muted">
            Instances get the policy when they start; running instances keep theirs until replaced.
          </p>
          {policy.env_overrides && (
            <p className="error" role="alert">
              ARGUS_MODEL_POLICY is set in the service environment and wins over the file. You can
              save here, but it has no effect until that variable is removed.
              {policy.file_revision && (
                <>
                  {" "}
                  File revision:{" "}
                  <code title={policy.file_revision}>{policy.file_revision.slice(0, 8)}</code>.
                </>
              )}
            </p>
          )}
          {SCOPES.map((scope) => (
            <ScopeEditor
              key={scope}
              scope={scope}
              chains={draft[scope]}
              onChange={(engine, chain) => setDraft(withChain(draft, scope, engine, chain))}
            />
          ))}
          <div className="toolbar">
            <button
              type="button"
              disabled={busy || !dirty}
              onClick={() => run(() => saveModelPolicy(draft), "Saved")}
            >
              Save
            </button>
            <button
              type="button"
              disabled={busy || !dirty}
              onClick={() => {
                setDraft(toDraft(policy));
                setFailure(undefined);
              }}
            >
              Discard changes
            </button>
            <button
              type="button"
              disabled={busy || policy.file_revision === null}
              onClick={() => run(() => restoreModelDefaults(), "Restored defaults")}
            >
              Restore defaults
            </button>
          </div>
          {failure && (
            <p className="error" role="alert">
              {failure}
            </p>
          )}
          {done && <p className="small">{done}</p>}
        </>
      )}
    </section>
  );
}

function ScopeEditor(props: {
  scope: Scope;
  chains: Record<string, string[]>;
  onChange: (engine: string, chain: string[] | null) => void;
}) {
  const { scope, chains, onChange } = props;
  const engines = Object.keys(chains).sort();
  const missing = KNOWN_ENGINES.filter((e) => !(e in chains));
  return (
    <fieldset>
      <legend>{TITLES[scope]}</legend>
      {engines.length === 0 && (
        <p className="muted small">No chains: every engine keeps its native default.</p>
      )}
      {engines.map((engine) => (
        <Chain
          key={engine}
          label={`${scope} ${engine}`}
          engine={engine}
          chain={chains[engine] ?? []}
          onChange={(chain) => onChange(engine, chain)}
        />
      ))}
      {missing.length > 0 && (
        <p className="small">
          Add engine:{" "}
          {missing.map((e) => (
            <button
              type="button"
              key={e}
              aria-label={`add ${scope} ${e}`}
              onClick={() => onChange(e, [])}
            >
              {e}
            </button>
          ))}
        </p>
      )}
    </fieldset>
  );
}

function Chain(props: {
  label: string;
  engine: string;
  chain: string[];
  onChange: (chain: string[] | null) => void;
}) {
  const { label, engine, chain, onChange } = props;
  const [text, setText] = useState("");
  const problem = text ? addProblem(chain, text.trim()) : null;
  const add = () => {
    onChange([...chain, text.trim()]);
    setText("");
  };
  return (
    <div>
      <strong>{engine}</strong>
      {chain.length === 0 && <span className="muted small"> empty: add a model before saving</span>}
      <ol>
        {chain.map((model, i) => (
          <li key={model}>
            <code>{model}</code>{" "}
            <button
              type="button"
              aria-label={`move ${model} up in ${label}`}
              disabled={i === 0}
              onClick={() => onChange(moved(chain, i, i - 1))}
            >
              ↑
            </button>
            <button
              type="button"
              aria-label={`move ${model} down in ${label}`}
              disabled={i === chain.length - 1}
              onClick={() => onChange(moved(chain, i, i + 1))}
            >
              ↓
            </button>
            <button
              type="button"
              aria-label={`remove ${model} from ${label}`}
              onClick={() => {
                const rest = chain.filter((m) => m !== model);
                onChange(rest.length ? rest : null);
              }}
            >
              Remove
            </button>
          </li>
        ))}
      </ol>
      <input
        aria-label={`new model for ${label}`}
        value={text}
        placeholder="model id"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && text.trim() && !problem) add();
        }}
      />{" "}
      <button
        type="button"
        aria-label={`add model to ${label}`}
        disabled={!text.trim() || !!problem}
        onClick={add}
      >
        Add
      </button>
      {problem && <span className="error small"> {problem}</span>}
    </div>
  );
}
