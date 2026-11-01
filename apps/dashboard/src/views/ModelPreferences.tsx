import { useEffect, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import { useLiveData } from "../live";
import {
  addProblem,
  compactDraft,
  type Draft,
  emptyChains,
  engineLabel,
  fetchModelPolicy,
  KNOWN_ENGINES,
  type ModelPolicy,
  moved,
  restoreModelDefaults,
  type Scope,
  saveModelPolicy,
  scopesOf,
  sourceLabel,
  toDraft,
  withChain,
} from "../models";

const TITLES: Record<Scope, string> = {
  harness: "Harness (NanoClaw)",
  implementation: "Implementation (argus-delegate)",
  trivial: "Trivial tasks and local leases (argus-delegate)",
  hard_harness: "Hard tasks: harness (NanoClaw)",
  hard_implementation: "Hard tasks: implementation (argus-delegate)",
};

/** Ordered model chains per engine, from GET/PUT/DELETE /api/models (global). */
export function ModelPreferences() {
  const { data, error, reload } = useLiveData((signal) => fetchModelPolicy(signal), []);
  const [policy, setPolicy] = useState<ModelPolicy>();
  const [draft, setDraft] = useState<Draft>();
  const [failure, setFailure] = useState<string>();
  const [done, setDone] = useState<string>();
  const action = useAction();

  const dirty = !!policy && !!draft && JSON.stringify(draft) !== JSON.stringify(toDraft(policy));
  const empties = draft ? emptyChains(draft) : [];

  // Live refreshes update the server view; unsaved edits are kept until saved or discarded.
  useEffect(() => {
    if (!data) return;
    if (dirty) setPolicy(data);
    else adopt(data);
  }, [data]);

  function adopt(p: ModelPolicy) {
    setPolicy(p);
    setDraft(toDraft(p));
  }

  async function run(fn: () => Promise<ModelPolicy>, verb: string, key: string) {
    await action.run(async () => {
      setFailure(undefined);
      setDone(undefined);
      try {
        const next = await fn();
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
      }
    }, key);
  }

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
        Changes apply to new instances. Running instances keep the policy they started with.
      </p>
      {error && <p className="error">{error.message}</p>}
      {!policy && !error && <p className="muted">Loading…</p>}
      {policy && draft && (
        <>
          <p className="small">
            Effective policy: <strong>{sourceLabel(policy.source)}</strong> · revision{" "}
            <code title={policy.revision}>{policy.revision.slice(0, 8)}</code>
            {policy.source === "defaults" && " · repository defaults, no file saved"}
            {policy.source === "file" && <span className="muted"> · {policy.path}</span>}
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
          <ModelPreferencesEditor draft={draft} onChange={setDraft} empties={empties} />
          <div className="toolbar model-actions">
            <ActionButton
              type="button"
              disabled={action.busy || !dirty}
              pending={action.is("save")}
              action={() => run(() => saveModelPolicy(compactDraft(draft)), "Saved", "save")}
            >
              Save
            </ActionButton>
            <button
              type="button"
              disabled={action.busy || !dirty}
              onClick={() => {
                setDraft(toDraft(policy));
                setFailure(undefined);
              }}
            >
              Discard changes
            </button>
            <ActionButton
              type="button"
              disabled={action.busy || policy.file_revision === null}
              pending={action.is("restore")}
              action={() => run(() => restoreModelDefaults(), "Restored defaults", "restore")}
            >
              Restore defaults
            </ActionButton>
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

const GROUPS: { title: string; scopes: Scope[] }[] = [
  { title: "Standard chains", scopes: ["harness", "implementation", "trivial"] },
  { title: "Hard tasks", scopes: ["hard_harness", "hard_implementation"] },
];

/** Shared chain editors used by the global ModelPreferences and the Settings project override. */
export function ModelPreferencesEditor(props: {
  draft: Draft;
  onChange: (d: Draft) => void;
  empties: string[];
}) {
  const { draft, onChange, empties } = props;
  const present = new Set(scopesOf(draft));
  const groups = GROUPS.map((group) => ({
    ...group,
    scopes: group.scopes.filter((scope) => present.has(scope)),
  })).filter((group) => group.scopes.length > 0);
  return (
    <div className="model-groups">
      {groups.map((group) => (
        <section key={group.title} className="model-group">
          <h4>{group.title}</h4>
          <div className="model-scopes">
            {group.scopes.map((scope) => (
              <ScopeEditor
                key={scope}
                scope={scope}
                chains={draft[scope] ?? {}}
                onChange={(engine, chain) => onChange(withChain(draft, scope, engine, chain))}
              />
            ))}
          </div>
        </section>
      ))}
      {empties.length > 0 && (
        <p className="muted small">
          Empty chains ({empties.join(", ")}) need a model before they are kept; they are omitted on
          save.
        </p>
      )}
    </div>
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
      <div className="model-scope-body">
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
          <p className="model-add-engines small">
            Add engine:{" "}
            {missing.map((e) => (
              <button
                type="button"
                key={e}
                aria-label={`add ${scope} ${e}`}
                onClick={() => onChange(e, [])}
              >
                {engineLabel(e)}
              </button>
            ))}
          </p>
        )}
      </div>
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
    <div className="model-chain">
      <div className="model-chain-head">
        <strong>{engineLabel(engine)}</strong>
        {chain.length === 0 && <span className="muted small">Add a model before saving</span>}
      </div>
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
      <div className="model-chain-add">
        <input
          aria-label={`new model for ${label}`}
          value={text}
          placeholder="model id"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && text.trim() && !problem) add();
          }}
        />
        <button
          type="button"
          aria-label={`add model to ${label}`}
          disabled={!text.trim() || !!problem}
          onClick={add}
        >
          Add
        </button>
        {problem && <span className="error small">{problem}</span>}
      </div>
    </div>
  );
}
