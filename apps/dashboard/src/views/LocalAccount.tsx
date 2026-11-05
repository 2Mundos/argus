import { type FormEvent, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import {
  api,
  type LocalCandidate as Candidate,
  call,
  type LocalDetection as Detection,
} from "../api";

/** Add a local model account: find a model server on this node, pick an endpoint
 * and a model, save (builtin-orchestrator spec 3.11). The account holds no secret. */
export function LocalAccount({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [found, setFound] = useState<Detection>();
  const [endpoint, setEndpoint] = useState<Candidate>();
  const [model, setModel] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string>();
  const action = useAction();

  async function detect() {
    setError(undefined);
    try {
      const result = await call(api.POST("/api/accounts/local/detect"));
      setFound(result);
      const first = result.candidates.find((c) => c.preferred) ?? result.candidates[0];
      choose(first, first?.models[0] ?? "");
    } catch (err) {
      setError((err as Error).message);
    }
  }

  function choose(candidate: Candidate | undefined, picked: string) {
    setEndpoint(candidate);
    setModel(picked);
    setName(picked ? `local-${picked}` : "");
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!endpoint || !model) return;
    setError(undefined);
    await action.run(async () => {
      try {
        await call(
          api.POST("/api/accounts", {
            body: {
              name,
              provider: "local",
              kind: "api_key",
              credential: { base_url: endpoint.base_url, model },
            },
          }),
        );
        onDone();
      } catch (err) {
        setError((err as Error).message);
      }
    });
  }

  return (
    <div className="overlay">
      <div className="dialog" role="dialog" aria-label="Add local model">
        <h2>Add local model</h2>
        <p className="muted small">
          A model server already running on this node: Ollama, LM Studio or llama.cpp. Nothing
          leaves the node. The built-in orchestrator needs a model with tool calling; detection does
          not test that. After saving, test it from the account's Endpoints.
        </p>
        <ActionButton type="button" action={() => detect()}>
          Detect
        </ActionButton>
        {found && found.candidates.length === 0 && (
          <p className="small" role="status">
            No model server answered on 127.0.0.1 or host.docker.internal (ports 11434, 1234, 8080).
            {found.hint && ` ${found.hint}`}
          </p>
        )}
        {found && found.candidates.length > 0 && (
          <form onSubmit={submit}>
            <fieldset>
              <legend>Endpoint</legend>
              {found.candidates.map((c) => (
                <label key={c.base_url} className="row">
                  <input
                    type="radio"
                    name="endpoint"
                    checked={endpoint?.base_url === c.base_url}
                    onChange={() => choose(c, c.models[0] ?? "")}
                  />
                  <span>
                    <code>{c.base_url}</code> ({c.server_hint}
                    {c.preferred ? ", reachable by workers" : ""})
                    {c.api_endpoint !== c.base_url && (
                      <span className="muted small">
                        {" "}
                        The Argus API uses <code>{c.api_endpoint}</code>.
                      </span>
                    )}
                    {c.note && <span className="muted small"> {c.note}</span>}
                  </span>
                </label>
              ))}
            </fieldset>
            {endpoint && (
              <label>
                Model
                <select value={model} onChange={(e) => choose(endpoint, e.target.value)}>
                  {endpoint.models.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <label>
              Name
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                maxLength={100}
              />
            </label>
            {error && <p className="error">{error}</p>}
            <div className="toolbar">
              <button type="button" onClick={onClose}>
                Cancel
              </button>
              <ActionButton
                type="submit"
                className="primary"
                pending={action.busy}
                disabled={!model}
              >
                Save
              </ActionButton>
            </div>
          </form>
        )}
        {!(found && found.candidates.length > 0) && (
          <>
            {error && <p className="error">{error}</p>}
            <div className="toolbar">
              <button type="button" onClick={onClose}>
                Cancel
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
