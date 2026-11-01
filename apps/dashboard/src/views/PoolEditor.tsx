import { type FormEvent, type ReactNode, useCallback, useEffect, useId, useState } from "react";
import { ActionButton, useAction } from "../ActionButton";
import {
  api,
  call,
  type Pool,
  type PoolPatch,
  type PoolUpsert,
  type TemplateCatalog,
} from "../api";
import {
  buildPool,
  type FieldErrors,
  FORM_FIELDS,
  type ImageChoices,
  type ImageRole,
  imageChoices,
  isStale,
  type PoolForm,
  poolErrors,
  toForm,
} from "../pools";

type CatalogState =
  | { status: "loading" }
  | { status: "ready"; catalog: TemplateCatalog }
  | { status: "error"; message: string };

/** Last catalog that loaded successfully in this page session. Survives a later failed
 * reload and a remount of the editor so host/agent cross-role checks keep configured names. */
let sessionCatalog: TemplateCatalog | undefined;

/** Live catalog state, a retry, and the last successfully loaded catalog (session + mount). */
function useImageCatalog(): [CatalogState, () => void, TemplateCatalog | undefined] {
  const [state, setState] = useState<CatalogState>({ status: "loading" });
  const [lastKnown, setLastKnown] = useState<TemplateCatalog | undefined>(sessionCatalog);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    call(api.GET("/api/fleet/templates", { signal: controller.signal }))
      .then((catalog) => {
        if (!Array.isArray(catalog?.nodes) || !Array.isArray(catalog?.templates)) {
          throw new Error("unexpected response");
        }
        sessionCatalog = catalog;
        setLastKnown(catalog);
        setState({ status: "ready", catalog });
      })
      .catch((err: Error) => {
        if (!controller.signal.aborted) setState({ status: "error", message: err.message });
      });
    return () => controller.abort();
  }, [attempt]);
  return [state, () => setAttempt((n) => n + 1), lastKnown];
}

/** Test-only: clear the session catalog between cases that must start with no prior load. */
export function resetSessionCatalogForTests(): void {
  sessionCatalog = undefined;
}

function Field(props: {
  id: string;
  label: string;
  help?: ReactNode;
  error?: string;
  children: (describedBy: string | undefined) => ReactNode;
}) {
  const { id, error } = props;
  const describedBy = [props.help ? `${id}-help` : "", error ? `${id}-error` : ""]
    .filter(Boolean)
    .join(" ");
  return (
    <div className="field">
      <label htmlFor={id}>{props.label}</label>
      {props.children(describedBy || undefined)}
      {props.help && (
        <p id={`${id}-help`} className="hint">
          {props.help}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} className="error small">
          {error}
        </p>
      )}
    </div>
  );
}

const CUSTOM = "\u0000custom";

/**
 * One image role: a choice from the verified catalog when there is one, always with a typed
 * value as the alternative. The stored value is always selectable, even when the catalog does
 * not list it, and nothing here ever replaces it unprompted.
 */
function ImageField(props: {
  id: string;
  label: string;
  help: ReactNode;
  role: ImageRole;
  value: string;
  stored: string;
  onChange: (value: string) => void;
  choices: ImageChoices | { kind: "loading" } | { kind: "error"; message: string };
  onRetry: () => void;
  error?: string;
}) {
  const { id, value, stored, choices } = props;
  const [custom, setCustom] = useState(false);
  const options = choices.kind === "options" ? choices.options : [];
  const listed = new Set(options.map((o) => o.value));
  const keepsStored = stored !== "" && !listed.has(stored);
  const inSelect = listed.has(value) || (keepsStored && value === stored);
  const typed = choices.kind !== "options" || custom || !inSelect;
  const picked = options.find((o) => o.value === value);
  const why =
    choices.kind === "loading"
      ? "Loading the image catalog…"
      : choices.kind === "error"
        ? `The image catalog could not be loaded (${choices.message}). The current value is kept; type a reference to change it.`
        : choices.kind === "empty"
          ? `${choices.reason} Type a reference to set one.`
          : choices.kind === "manual-only"
            ? `${choices.reason} There is no verified list to choose from; type a reference.`
            : undefined;

  return (
    <Field id={id} label={props.label} help={props.help} error={props.error}>
      {(describedBy) => (
        <>
          {choices.kind === "options" && (
            <select
              id={id}
              value={typed && !inSelect ? CUSTOM : custom ? CUSTOM : value}
              aria-describedby={describedBy}
              onChange={(e) => {
                if (e.target.value === CUSTOM) setCustom(true);
                else {
                  setCustom(false);
                  props.onChange(e.target.value);
                }
              }}
            >
              {keepsStored && (
                <option value={stored}>{stored} (current, not in the catalog)</option>
              )}
              {options.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.value}
                </option>
              ))}
              <option value={CUSTOM}>Other: type a reference…</option>
            </select>
          )}
          {typed && (
            <input
              id={choices.kind === "options" ? `${id}-manual` : id}
              aria-label={
                choices.kind === "options" ? `${props.label} (typed reference)` : undefined
              }
              value={value}
              spellCheck={false}
              autoCapitalize="off"
              aria-describedby={describedBy}
              aria-invalid={props.error ? true : undefined}
              onChange={(e) => props.onChange(e.target.value)}
            />
          )}
          {picked && <p className="hint">{picked.note}</p>}
          {why && <p className="hint">{why}</p>}
          {choices.kind === "error" && (
            <button type="button" className="link" onClick={props.onRetry}>
              Retry loading the catalog
            </button>
          )}
        </>
      )}
    </Field>
  );
}

const SANDBOX = "sandboxed (own Docker daemon)";

/** Create (no `pool`) or edit one pool. Edit works from the pool as it was opened, never from
 * live refreshes, so a draft is never overwritten; a stale version keeps it. */
export function PoolEditor({ pool, onClose }: { pool?: Pool; onClose: () => void }) {
  const uid = useId();
  const [base, setBase] = useState(pool);
  const [form, setForm] = useState<PoolForm>(() => toForm(pool));
  const [errors, setErrors] = useState<FieldErrors>({});
  const [conflict, setConflict] = useState<string>();
  const action = useAction();
  /** Bumped on Discard/Keep so ImageField remounts and its "Other" local state matches the form. */
  const [rebaseKey, setRebaseKey] = useState(0);
  const [catalog, reloadCatalog, lastKnownCatalog] = useImageCatalog();
  const set = <K extends keyof PoolForm>(key: K, value: PoolForm[K]) =>
    setForm((f) => ({ ...f, [key]: value }));
  const id = (name: string) => `${uid}-${name}`;
  const nanoclaw = base?.runtime === "nanoclaw";
  const runtime = base?.runtime ?? "plain";
  // Prefer the live catalog; if a reload failed, keep using the last successful one for
  // host/agent cross-role checks so a catalog outage cannot skip that guard.
  const roleCatalog = catalog.status === "ready" ? catalog.catalog : lastKnownCatalog;
  const rolesUnverified = nanoclaw && catalog.status !== "loading" && roleCatalog === undefined;

  function choices(role: ImageRole) {
    if (catalog.status === "loading") return { kind: "loading" } as const;
    if (catalog.status === "error") return { kind: "error", message: catalog.message } as const;
    return imageChoices(catalog.catalog, role, {
      runtime,
      target: form.target,
      dockerHost: form.dockerHost,
    });
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setConflict(undefined);
    const built = buildPool(base, form, roleCatalog);
    setErrors(built.errors);
    if (!built.body) return;
    if (built.unchanged) return onClose();
    await action.run(async () => {
      try {
        if (base) {
          await call(
            api.PATCH("/api/pools/{name}", {
              params: { path: { name: base.name } },
              body: { expected_version: base.version, ...built.body } as PoolPatch,
            }),
          );
        } else {
          await call(
            api.PUT("/api/pools/{name}", {
              params: { path: { name: form.name } },
              body: built.body as PoolUpsert,
            }),
          );
        }
        onClose();
      } catch (err) {
        if (isStale(err)) setConflict(err.details[0] ?? "");
        else setErrors(poolErrors(err));
      }
    });
  }

  async function latest(): Promise<Pool | undefined> {
    if (!base) return undefined;
    try {
      return await call(api.GET("/api/pools/{name}", { params: { path: { name: base.name } } }));
    } catch (err) {
      setErrors({ form: `Could not read the latest pool: ${(err as Error).message}` });
      return undefined;
    }
  }

  async function reload() {
    const fresh = await latest();
    if (!fresh) return;
    setBase(fresh);
    setForm(toForm(fresh));
    setConflict(undefined);
    setErrors({});
    setRebaseKey((n) => n + 1);
  }

  /** The operator's own edits win; every field they did not touch takes the latest value. */
  async function keepEditing() {
    const fresh = await latest();
    if (!fresh) return;
    const before = toForm(base);
    const theirs = toForm(fresh);
    const merged = { ...theirs } as Record<string, unknown>;
    for (const f of FORM_FIELDS) if (form[f] !== before[f]) merged[f] = form[f];
    setBase(fresh);
    setForm(merged as unknown as PoolForm);
    setConflict(undefined);
    setErrors({});
    setRebaseKey((n) => n + 1);
  }

  const isDocker = form.target === "docker";
  const mode = base?.docker.runtime
    ? `${SANDBOX}, runtime ${base.docker.runtime}`
    : "host Docker socket";

  return (
    <div className="overlay">
      <form
        className="dialog pool-editor"
        role="dialog"
        aria-modal="true"
        aria-labelledby={id("title")}
        onSubmit={submit}
      >
        <h2 id={id("title")}>{base ? `Edit pool ${base.name}` : "New pool (advanced)"}</h2>
        {conflict !== undefined && (
          <div className="error" role="alert">
            <p>
              This pool changed while you were editing (now at version {conflict || "newer"}). Your
              draft is kept.
            </p>
            <div className="actions left">
              <button type="button" onClick={() => void reload()}>
                Discard my edits and reload
              </button>
              <button type="button" className="primary" onClick={() => void keepEditing()}>
                Keep my edits on the latest version
              </button>
            </div>
          </div>
        )}
        {errors.form && (
          <p className="error" role="alert">
            {errors.form}
          </p>
        )}
        {!base && (
          <p className="hint">
            This form creates a plain worker pool. NanoClaw pools are created with New worker, which
            builds their options from the vetted template.
          </p>
        )}
        {base && (
          <p className="hint">
            {base.target}/{base.runtime}
            {nanoclaw && ` · ${mode}`}
            {base.template && ` · template ${base.template}`}. Runtime, target and the NanoClaw
            install, socket and gateway options cannot be changed here; they are kept as stored.
          </p>
        )}
        <Field id={id("name")} label="Name" error={errors.name}>
          {(d) => (
            <input
              id={id("name")}
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              disabled={Boolean(base)}
              aria-describedby={d}
              required
            />
          )}
        </Field>

        <fieldset>
          <legend>Images</legend>
          {rolesUnverified && (
            <p className="hint" role="status">
              The image catalog is unavailable, so host and agent role names cannot be verified
              against configured images. The two fields must still differ; the server also rejects
              equal or swapped role images. Retry loading the catalog to verify before saving when
              you can.
            </p>
          )}
          <ImageField
            key={`image-${rebaseKey}`}
            id={id("image")}
            label="Host image"
            role="host"
            help={
              nanoclaw
                ? "The Docker image of the runtime host Argus starts for each instance (pool image): the NanoClaw host image. Not the agent image."
                : "The Docker image Argus starts for each instance (pool image). It must speak the Argus worker API."
            }
            value={form.image}
            stored={base?.image ?? ""}
            onChange={(v) => set("image", v)}
            choices={choices("host")}
            onRetry={reloadCatalog}
            error={errors.image}
          />
          {nanoclaw && (
            <ImageField
              key={`agentImage-${rebaseKey}`}
              id={id("agentImage")}
              label="NanoClaw agent image"
              role="agent"
              help={
                base?.docker.runtime
                  ? "The image of the agent containers the host starts (nanoclaw.agent_image). Sandboxed pools load it from the image archive into the instance's own daemon. Not the host image."
                  : "The image of the agent containers the host starts (nanoclaw.agent_image). It must already be on the host's Docker daemon. Not the host image."
              }
              value={form.agentImage}
              stored={base?.nanoclaw?.agent_image ?? ""}
              onChange={(v) => set("agentImage", v)}
              choices={choices("agent")}
              onRetry={reloadCatalog}
              error={errors.agentImage}
            />
          )}
          <label className="check">
            <input
              type="checkbox"
              checked={form.allowMutableImages}
              onChange={(e) => set("allowMutableImages", e.target.checked)}
            />
            Allow mutable (tag-only) images that are missing locally
          </label>
        </fieldset>

        <fieldset>
          <legend>Capacity</legend>
          <div className="row">
            <Field id={id("desired")} label="Desired replicas" error={errors.desired}>
              {(d) => (
                <input
                  id={id("desired")}
                  type="number"
                  min={0}
                  value={form.desired}
                  aria-describedby={d}
                  onChange={(e) => set("desired", e.target.value)}
                />
              )}
            </Field>
            <Field id={id("max")} label="Max replicas (cap)" error={errors.max}>
              {(d) => (
                <input
                  id={id("max")}
                  type="number"
                  min={0}
                  value={form.max}
                  aria-describedby={d}
                  onChange={(e) => set("max", e.target.value)}
                />
              )}
            </Field>
            {!nanoclaw && (
              <Field id={id("per")} label="Agents per instance" error={errors.agentsPerInstance}>
                {(d) => (
                  <input
                    id={id("per")}
                    type="number"
                    min={1}
                    max={50}
                    value={form.agentsPerInstance}
                    aria-describedby={d}
                    onChange={(e) => set("agentsPerInstance", e.target.value)}
                  />
                )}
              </Field>
            )}
          </div>
          <div className="row">
            <Field id={id("cpus")} label="CPUs per instance (empty: no limit)" error={errors.cpus}>
              {(d) => (
                <input
                  id={id("cpus")}
                  inputMode="decimal"
                  value={form.cpus}
                  aria-describedby={d}
                  onChange={(e) => set("cpus", e.target.value)}
                />
              )}
            </Field>
            <Field
              id={id("mem")}
              label="Memory MB per instance (empty: no limit)"
              error={errors.memoryMb}
            >
              {(d) => (
                <input
                  id={id("mem")}
                  inputMode="numeric"
                  value={form.memoryMb}
                  aria-describedby={d}
                  onChange={(e) => set("memoryMb", e.target.value)}
                />
              )}
            </Field>
          </div>
          <Field id={id("caps")} label="Capabilities (comma-separated)" error={errors.capabilities}>
            {(d) => (
              <input
                id={id("caps")}
                value={form.capabilities}
                aria-describedby={d}
                onChange={(e) => set("capabilities", e.target.value)}
              />
            )}
          </Field>
        </fieldset>

        <Field
          id={id("env")}
          label="Environment (KEY=value per line)"
          error={errors.env}
          help="Stored values are write-only and show as ***. Leave a *** line as it is to keep its value; remove the line to delete the variable. *** is never sent to the server."
        >
          {(d) => (
            <textarea
              id={id("env")}
              rows={3}
              value={form.env}
              spellCheck={false}
              aria-describedby={d}
              onChange={(e) => set("env", e.target.value)}
            />
          )}
        </Field>

        <fieldset>
          <legend>Placement</legend>
          {!base && (
            <Field id={id("target")} label="Target">
              {() => (
                <select
                  id={id("target")}
                  value={form.target}
                  onChange={(e) => set("target", e.target.value as PoolForm["target"])}
                >
                  <option value="docker">docker</option>
                  <option value="kubernetes">kubernetes (one Pod per instance)</option>
                </select>
              )}
            </Field>
          )}
          {isDocker ? (
            <>
              <div className="row">
                <Field
                  id={id("dhost")}
                  label="Docker host (empty: local)"
                  error={errors.dockerHost}
                >
                  {(d) => (
                    <input
                      id={id("dhost")}
                      value={form.dockerHost}
                      placeholder="ssh://user@host"
                      aria-describedby={d}
                      onChange={(e) => set("dockerHost", e.target.value)}
                    />
                  )}
                </Field>
                <Field
                  id={id("dnet")}
                  label="Docker network (empty: default)"
                  error={errors.dockerNetwork}
                >
                  {(d) => (
                    <input
                      id={id("dnet")}
                      value={form.dockerNetwork}
                      aria-describedby={d}
                      onChange={(e) => set("dockerNetwork", e.target.value)}
                    />
                  )}
                </Field>
              </div>
              <Field id={id("hosts")} label="Extra hosts" error={errors.extraHosts}>
                {(d) => (
                  <input
                    id={id("hosts")}
                    value={form.extraHosts}
                    aria-describedby={d}
                    onChange={(e) => set("extraHosts", e.target.value)}
                  />
                )}
              </Field>
            </>
          ) : (
            <div className="row">
              <Field
                id={id("kctx")}
                label="Kubeconfig context (or in-cluster)"
                error={errors.k8sContext}
              >
                {(d) => (
                  <input
                    id={id("kctx")}
                    value={form.k8sContext}
                    aria-describedby={d}
                    onChange={(e) => set("k8sContext", e.target.value)}
                  />
                )}
              </Field>
              <Field id={id("kns")} label="Namespace" error={errors.k8sNamespace}>
                {(d) => (
                  <input
                    id={id("kns")}
                    value={form.k8sNamespace}
                    pattern="[a-z0-9]([\-a-z0-9]*[a-z0-9])?"
                    aria-describedby={d}
                    onChange={(e) => set("k8sNamespace", e.target.value)}
                  />
                )}
              </Field>
            </div>
          )}
        </fieldset>

        <div className="actions">
          <button type="button" disabled={action.busy} onClick={onClose}>
            Cancel
          </button>
          <ActionButton
            type="submit"
            className="primary"
            pending={action.busy}
            pendingLabel="Saving…"
          >
            Save
          </ActionButton>
        </div>
      </form>
    </div>
  );
}
