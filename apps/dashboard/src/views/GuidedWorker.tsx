import { type FormEvent, useCallback, useEffect, useId, useRef, useState } from "react";
import {
  api,
  type CreateWorkerRequest,
  call,
  type TemplateCatalog,
  type WorkerTemplate,
} from "../api";
import {
  blockingReason,
  buildRequest,
  defaultCap,
  type GuidedState,
  initialState,
  offeredImages,
  readinessFor,
  select,
  verdict,
} from "../guided";

/** The guided form: pick a worker template, confirm, create. It only edits local
 * state; nothing is created until Create is pressed. */
export function GuidedWorkerForm(props: {
  catalog: TemplateCatalog;
  onCreate: (request: CreateWorkerRequest) => Promise<void>;
  onCancel?: () => void;
}) {
  const { catalog } = props;
  const [state, setState] = useState<GuidedState>(() => initialState(catalog));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const inFlight = useRef(false); // a double click submits once
  const template = catalog.templates.find((t) => t.id === state.template);
  const readiness = readinessFor(template, state.node);
  const images = offeredImages(catalog, state);
  const gate = verdict(catalog, state, busy);
  const set = (change: Partial<GuidedState>) => setState((s) => ({ ...s, ...change }));

  // The catalog is re-read on "Re-check"; keep the choice if it still exists.
  useEffect(() => {
    setState((s) => {
      const t = catalog.templates.find((x) => x.id === s.template);
      if (!t) return initialState(catalog); // nothing chosen yet, or it vanished
      const next = select(catalog, s, {});
      if (!s.nameEdited) next.name = t.defaults.name; // the suggestion moves on once taken
      return next;
    });
  }, [catalog]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!gate.ready || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(undefined);
    try {
      await props.onCreate(buildRequest(catalog, state));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  const noun = state.quantity === 1 ? "worker" : "workers";
  return (
    <form className="guided" onSubmit={submit}>
      {!catalog.public_url_configured && (
        <p className="error small">ARGUS_PUBLIC_URL is not set: no worker can reach Argus.</p>
      )}
      <fieldset className="choices">
        <legend>1. What kind of worker?</legend>
        {catalog.templates.map((t) => (
          <TemplateChoice
            key={t.id}
            template={t}
            node={state.node}
            checked={t.id === state.template}
            onPick={() => setState((s) => select(catalog, s, { template: t.id }))}
          />
        ))}
      </fieldset>

      {!template && <p className="small muted">Choose a worker type to continue.</p>}
      {template?.warning && (
        <div className="warning" role="alert">
          <strong>{template.warning}</strong>
          <label className="check">
            <input
              type="checkbox"
              checked={state.acknowledged}
              onChange={(e) => set({ acknowledged: e.target.checked })}
            />
            I understand and accept this
          </label>
        </div>
      )}
      {template && readiness && !readiness.ready && (
        <p className="blocked" role="status">
          <strong>Not ready:</strong> {blockingReason(readiness)}
        </p>
      )}
      {template && (
        <ul className="notes small muted">
          {template.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
      {template && readiness && (
        <details className="setup">
          <summary>Setup details</summary>
          {readiness.missing.length > 0 && (
            <>
              <p className="small">Missing on this node:</p>
              <ul className="small">
                {readiness.missing.map((m) => (
                  <li key={m}>{m}</li>
                ))}
              </ul>
            </>
          )}
          <p className="small muted">{template.setup.join(" ")}</p>
        </details>
      )}

      {template && (
        <fieldset>
          <legend>2. Details</legend>
          <div className="row">
            <label>
              Name
              <input
                value={state.name}
                onChange={(e) => set({ name: e.target.value, nameEdited: true })}
                required
              />
            </label>
            <label>
              How many
              <input
                type="number"
                min={1}
                value={state.quantity}
                onChange={(e) => set({ quantity: Number(e.target.value) })}
              />
            </label>
          </div>
          {catalog.nodes.length > 1 && (
            <label>
              Where
              <select
                value={state.node}
                onChange={(e) => setState((s) => select(catalog, s, { node: e.target.value }))}
              >
                {catalog.nodes.map((n) => (
                  <option key={n.id} value={n.id}>
                    {n.label}
                    {n.available ? "" : " (unavailable)"}
                  </option>
                ))}
              </select>
            </label>
          )}
          {template?.runtime === "plain" && images.length > 0 && (
            <label>
              Image
              <select value={state.image} onChange={(e) => set({ image: e.target.value })}>
                {images.length > 1 && <option value="">Choose…</option>}
                {images.map((i) => (
                  <option key={i.image} value={i.image}>
                    {i.image}
                    {i.capabilities.length > 0 ? ` (${i.capabilities.join(", ")})` : ""}
                  </option>
                ))}
              </select>
            </label>
          )}
        </fieldset>
      )}

      <details>
        <summary>Advanced</summary>
        <label>
          Capabilities (comma-separated)
          <input
            value={
              state.capabilities ??
              (
                images.find((i) => i.image === state.image)?.capabilities ??
                template?.defaults.capabilities ??
                []
              ).join(", ")
            }
            onChange={(e) => set({ capabilities: e.target.value })}
          />
        </label>
        <label>
          Environment (KEY=value per line)
          <textarea
            rows={3}
            value={state.env ?? ""}
            onChange={(e) => set({ env: e.target.value })}
          />
        </label>
        <label>
          Extra hosts (comma-separated)
          <input
            value={state.extraHosts ?? (template?.defaults.extra_hosts ?? []).join(", ")}
            onChange={(e) => set({ extraHosts: e.target.value })}
          />
        </label>
        <label>
          Max replicas (cap)
          <input
            type="number"
            min={1}
            value={state.maxReplicas ?? defaultCap(catalog, state)}
            onChange={(e) => set({ maxReplicas: e.target.value })}
          />
        </label>
        <p className="small muted">
          Image, runtime and security settings come from the template. For anything else use{" "}
          <em>New pool (advanced)</em> in Pools.
        </p>
      </details>

      {error && <p className="error">{error}</p>}
      {template && !gate.ready && gate.reason && <p className="small muted">{gate.reason}</p>}
      <div className="actions">
        {props.onCancel && (
          <button type="button" onClick={props.onCancel}>
            Cancel
          </button>
        )}
        <button type="submit" className="primary" disabled={!gate.ready}>
          {busy ? "Creating…" : `Create ${state.quantity} ${noun}`}
        </button>
      </div>
    </form>
  );
}

function TemplateChoice(props: {
  template: WorkerTemplate;
  node: string;
  checked: boolean;
  onPick: () => void;
}) {
  const { template: t } = props;
  const id = useId();
  const ready = readinessFor(t, props.node)?.ready ?? false;
  return (
    <label className={`choice${props.checked ? " selected" : ""}`}>
      <input
        type="radio"
        name="template"
        checked={props.checked}
        onChange={props.onPick}
        aria-labelledby={`${id}-name`}
        aria-describedby={`${id}-desc`}
      />
      <span className="choice-text">
        <span className="choice-head">
          <strong id={`${id}-name`}>{t.name}</strong>
          <span className={`badge ${ready ? "agent-active" : "agent-lost"}`}>
            {ready ? "ready" : "not ready"}
          </span>
          {t.warning && <span className="badge agent-lost">not sandboxed</span>}
        </span>
        <span id={`${id}-desc`} className="small muted">
          {t.description}
        </span>
      </span>
    </label>
  );
}

/** Loads the catalog, creates on confirmation, and says plainly that success
 * means *requested*: the worker is tracked by its instance, not assumed. */
export function NewWorker(props: { onDone?: () => void; onCancel?: () => void }) {
  const [catalog, setCatalog] = useState<TemplateCatalog>();
  const [loadError, setLoadError] = useState<string>();
  const [done, setDone] = useState<string>();
  const [created, setCreated] = useState(0); // remounts the form: fresh defaults

  const load = useCallback(async () => {
    setLoadError(undefined);
    try {
      setCatalog(await call(api.GET("/api/fleet/templates")));
    } catch (err) {
      setLoadError((err as Error).message);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);

  async function create(body: CreateWorkerRequest) {
    const res = await call(api.POST("/api/fleet/workers", { body }));
    setDone(
      `Requested ${res.requested} worker${res.requested === 1 ? "" : "s"} in pool ${res.pool.name}. ` +
        "They are not running yet: follow their state below.",
    );
    setCreated((n) => n + 1);
    props.onDone?.();
    void load();
  }

  return (
    <div className="card">
      <div className="toolbar">
        <h3>New worker</h3>
        <button type="button" className="link" onClick={() => void load()}>
          Re-check node
        </button>
      </div>
      {loadError && <p className="error">Could not read what this node can run: {loadError}</p>}
      {!catalog && !loadError && <p className="muted small">Checking what this node can run…</p>}
      {done && <p className="small">{done}</p>}
      {catalog && (
        <GuidedWorkerForm
          key={created}
          catalog={catalog}
          onCreate={create}
          onCancel={props.onCancel}
        />
      )}
    </div>
  );
}
