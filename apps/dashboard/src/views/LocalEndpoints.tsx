import { type FormEvent, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import {
  type Account,
  api,
  call,
  type LocalAccountView,
  type LocalCheck,
  type LocalTestResult,
} from "../api";

type Body = {
  api_base_url?: string | null;
  base_url?: string;
  context_tokens?: number | null;
  model?: string | null;
};
type TestKind = "discovery" | "inference" | "tools";

const SOURCE: Record<LocalAccountView["api_endpoint_source"], string> = {
  api_base_url: "set here",
  derived: "derived: the host side of the Docker bridge forwarder",
  base_url: "the workers' endpoint",
};

const MODEL_SOURCE: Record<string, string> = {
  "orchestrator.model": "from orchestrator.model in Settings",
  account: "the account's model",
  served: "the server's first model",
};

type Status = NonNullable<LocalCheck["status"]>;

const STATUS: Record<Status, string> = {
  passed: "passed",
  failed: "failed",
  skipped: "skipped",
  not_run: "not run",
};

const BADGE: Record<Status, string> = {
  passed: "account-available",
  failed: "account-disabled",
  skipped: "account-exhausted",
  not_run: "",
};

/** One check's status, model and Argus's own sentence (never server text). */
function Check({ label, check }: { label: string; check: LocalCheck }) {
  const status: Status = check.status ?? "not_run";
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        <span className={`badge ${BADGE[status]}`} data-status={status}>
          {STATUS[status]}
        </span>
        {check.model && <code className="small"> {check.model}</code>}
        {check.latency_ms != null && status !== "not_run" && (
          <span className="muted small"> {check.latency_ms} ms</span>
        )}
        {check.failure && <span className="muted small"> ({check.failure})</span>}
        {check.detail && <span className="small"> {check.detail}</span>}
      </dd>
    </div>
  );
}

/** Edit, test and reset a local account's endpoints and model (local harness
 * spec, "Editor" and "Admin test"). Workers and the Argus API reach the model
 * on different addresses; a test never sends a completion unless asked. */
export function LocalEndpoints({
  account,
  onClose,
  onSaved,
}: {
  account: Account;
  onClose: () => void;
  onSaved: (message: string) => void;
}) {
  const [view, setView] = useState<LocalAccountView | null | undefined>(account.local);
  const [baseUrl, setBaseUrl] = useState(view?.base_url ?? "");
  const [apiBaseUrl, setApiBaseUrl] = useState(view?.api_base_url ?? "");
  const [model, setModel] = useState(view?.model ?? "");
  const [result, setResult] = useState<LocalTestResult>();
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const action = useAction();

  async function put(body: Body, message: string, key: string) {
    setError(undefined);
    setNotice(undefined);
    await action.run(async () => {
      try {
        const saved = await call(
          api.PUT("/api/accounts/{account_id}/local", {
            params: { path: { account_id: account.id } },
            body,
          }),
        );
        const local = saved.local ?? null;
        setView(local);
        setResult(undefined); // an edit makes the last test stale
        setBaseUrl(local?.base_url ?? baseUrl);
        setApiBaseUrl(local?.api_base_url ?? "");
        setModel(local?.model ?? "");
        setNotice(message);
        onSaved(message);
      } catch (err) {
        setError((err as Error).message);
      }
    }, key);
  }

  async function save(e: FormEvent) {
    e.preventDefault();
    if (!view) return;
    const body: Body = {};
    if (baseUrl.trim() !== view.base_url) body.base_url = baseUrl.trim();
    const endpoint = apiBaseUrl.trim() || null;
    if (endpoint !== (view.api_base_url ?? null)) body.api_base_url = endpoint;
    const picked = model.trim() || null;
    if (picked !== (view.model ?? null)) body.model = picked;
    if (Object.keys(body).length === 0) {
      setNotice("Nothing changed.");
      return;
    }
    await put(body, `Saved the endpoints of “${account.name}”.`, "save");
  }

  async function reset() {
    await put(
      { api_base_url: null, model: null, context_tokens: null },
      `Reset “${account.name}”: the default API endpoint and the server's model.`,
      "reset",
    );
  }

  async function test(kind: TestKind) {
    setError(undefined);
    setNotice(undefined);
    await action.run(async () => {
      try {
        const got = await call(
          api.POST("/api/accounts/{account_id}/local/test", {
            params: { path: { account_id: account.id } },
            body: { inference: kind === "inference", tool_calling: kind === "tools" },
          }),
        );
        setResult(got);
      } catch (err) {
        setError((err as Error).message);
      }
    }, `test:${kind}`);
  }

  if (!view) {
    return (
      <div className="overlay">
        <div className="dialog" role="dialog" aria-label={`Local endpoints of ${account.name}`}>
          <h2>Local endpoints of {account.name}</h2>
          <p className="error" role="alert">
            The stored endpoint is missing or invalid. Replace the account's credential first.
          </p>
          <div className="actions">
            <button type="button" onClick={onClose}>
              Close
            </button>
          </div>
        </div>
      </div>
    );
  }

  const discovery = result?.discovery ?? view.discovery;
  const inference = result?.inference ?? view.inference;
  const orchestrator = result?.orchestrator ?? view.orchestrator;
  const served = result?.served_models ?? view.served_models ?? [];
  const warnings = result?.warnings ?? view.warnings ?? [];
  return (
    <div className="overlay">
      <form
        className="dialog wide"
        onSubmit={save}
        aria-label={`Local endpoints of ${account.name}`}
        noValidate
      >
        <h2>Local endpoints of {account.name}</h2>
        <p className="muted small">
          Worker containers and the Argus API do not share a network: on a host install
          host.docker.internal resolves only inside containers. Only 127.0.0.1, localhost, ::1 and
          host.docker.internal are accepted.
        </p>
        <label>
          Worker endpoint (containers)
          <input
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="http://host.docker.internal:8080"
            required
          />
        </label>
        <label>
          API endpoint (built-in orchestrator, health probe, tests)
          <input
            value={apiBaseUrl}
            onChange={(e) => setApiBaseUrl(e.target.value)}
            placeholder={view.api_endpoint}
          />
        </label>
        <p className="muted small" data-testid="api-endpoint">
          The API uses <code>{view.api_endpoint}</code> ({SOURCE[view.api_endpoint_source]}). Empty:
          the default.
        </p>
        <label>
          Model (empty: the server's model)
          <input
            value={model}
            onChange={(e) => setModel(e.target.value)}
            list={`served-${account.id}`}
          />
          <datalist id={`served-${account.id}`}>
            {served.map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </label>
        {view.orchestrator_model && (
          <p className="muted small">
            The built-in orchestrator runs <code>{view.orchestrator_model}</code> (
            {MODEL_SOURCE[view.orchestrator_model_source ?? ""] ?? "unknown"}).
          </p>
        )}
        {warnings.length > 0 && (
          <ul className="small" aria-label="Warnings">
            {warnings.map((w) => (
              <li key={w} className="error">
                {w}
              </li>
            ))}
          </ul>
        )}
        <h3>Checks from the Argus API</h3>
        <dl className="facts" aria-label="Checks">
          <Check label="Discovery" check={discovery} />
          <Check label="Inference" check={inference} />
          <Check label="Tool calling (orchestrator)" check={orchestrator} />
        </dl>
        {result?.worker_note && <p className="muted small">{result.worker_note}</p>}
        <p className="muted small">
          Discovery only lists models. Inference and tool calling each send one short completion (at
          most 256 output tokens) and run only when you press them.
        </p>
        <div className="toolbar">
          <ActionButton
            type="button"
            pending={action.is("test:discovery")}
            disabled={action.busy}
            action={() => test("discovery")}
          >
            Test connection
          </ActionButton>
          <ActionButton
            type="button"
            pending={action.is("test:inference")}
            disabled={action.busy}
            action={() => test("inference")}
          >
            Test inference
          </ActionButton>
          <ActionButton
            type="button"
            pending={action.is("test:tools")}
            disabled={action.busy}
            action={() => test("tools")}
          >
            Test tool calling
          </ActionButton>
        </div>
        {notice && (
          <p className="small" role="status">
            {notice}
          </p>
        )}
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="actions">
          <button type="button" disabled={action.busy} onClick={onClose}>
            Close
          </button>
          <ActionButton
            type="button"
            pending={action.is("reset")}
            disabled={action.busy}
            action={() => reset()}
          >
            Reset to defaults
          </ActionButton>
          <ActionButton
            type="submit"
            className="primary"
            pending={action.is("save")}
            disabled={action.busy}
          >
            Save
          </ActionButton>
        </div>
      </form>
    </div>
  );
}
