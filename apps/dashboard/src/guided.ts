import type { CreateWorkerRequest, TemplateCatalog, WorkerTemplate } from "./api";

/** The guided form's state. Everything starts from the server's catalog. */
export interface GuidedState {
  template: string;
  node: string;
  name: string;
  nameEdited: boolean;
  quantity: number;
  image: string;
  /** Advanced fields; `null` until the user opens and changes them. */
  capabilities: string | null;
  env: string | null;
  extraHosts: string | null;
  /** Cap override as typed; `null` = leave the server default. */
  maxReplicas: string | null;
  /** The person accepted the template's warning (re-asked on every template change). */
  acknowledged: boolean;
}

export function readinessFor(template: WorkerTemplate | undefined, node: string) {
  return template?.readiness.find((r) => r.node === node);
}

/** Nothing is preselected unless it is safe and ready: the first ready template
 * without a security warning. A template that carries a warning is a deliberate
 * choice, and a not-ready one is never a default. Otherwise: no selection. */
export function initialState(catalog: TemplateCatalog): GuidedState {
  const template = catalog.templates.find((t) => t.ready && !t.warning);
  const node =
    readinessFor(template, catalog.default_node)?.ready || !template
      ? catalog.default_node
      : (template.readiness.find((r) => r.ready)?.node ?? catalog.default_node);
  return select(
    catalog,
    {
      template: template?.id ?? "",
      node,
      name: template?.defaults.name ?? "",
      nameEdited: false,
      quantity: template?.defaults.quantity ?? 1,
      image: "",
      capabilities: null,
      env: null,
      extraHosts: null,
      maxReplicas: null,
      acknowledged: false,
    },
    {},
  );
}

/** The cap the server applies when none is sent: max(quantity, template default). */
export function defaultCap(catalog: TemplateCatalog, state: GuidedState): number {
  const template = catalog.templates.find((t) => t.id === state.template);
  return Math.max(state.quantity, template?.defaults.max_replicas ?? 1);
}

/** Switching template or node re-derives what the user did not edit. */
export function select(
  catalog: TemplateCatalog,
  state: GuidedState,
  change: Partial<Pick<GuidedState, "template" | "node">>,
): GuidedState {
  // A new template or node re-derives the advanced defaults; a plain refresh
  // (empty change) keeps what the user typed.
  const changed = Object.keys(change).length > 0;
  const next = {
    ...state,
    ...change,
    image: changed ? "" : state.image,
    ...(changed && {
      capabilities: null,
      extraHosts: null,
      maxReplicas: null,
      acknowledged: false,
    }),
  };
  const template = catalog.templates.find((t) => t.id === next.template);
  if (change.template && !state.nameEdited) next.name = template?.defaults.name ?? "";
  const images = readinessFor(template, next.node)?.images ?? [];
  const only = images.length === 1 ? images[0] : undefined;
  if (only) next.image = only.image;
  return next;
}

export function offeredImages(catalog: TemplateCatalog, state: GuidedState) {
  const template = catalog.templates.find((t) => t.id === state.template);
  return readinessFor(template, state.node)?.images ?? [];
}

export interface Verdict {
  ready: boolean;
  missing: string[];
  reason?: string;
}

/** Whether Create may be pressed, and if not, exactly why. */
export function verdict(catalog: TemplateCatalog, state: GuidedState, busy: boolean): Verdict {
  const template = catalog.templates.find((t) => t.id === state.template);
  const readiness = readinessFor(template, state.node);
  if (!template) return { ready: false, missing: [], reason: "Choose a worker type to continue." };
  if (!readiness) return { ready: false, missing: [], reason: "Pick a node." };
  if (!readiness.ready) return { ready: false, missing: readiness.missing };
  if (busy) return { ready: false, missing: [], reason: "Creating…" };
  if (template.warning && !state.acknowledged)
    return { ready: false, missing: [], reason: "Confirm that you accept the warning above." };
  if (template.runtime === "plain" && !state.image && readiness.images.length > 1)
    return { ready: false, missing: [], reason: "Choose an image." };
  if (!/^[a-z0-9][a-z0-9-]{0,39}$/.test(state.name))
    return { ready: false, missing: [], reason: "Name: lowercase letters, digits and dashes." };
  if (!Number.isInteger(state.quantity) || state.quantity < 1)
    return { ready: false, missing: [], reason: "Quantity must be at least 1." };
  if (state.maxReplicas !== null) {
    const cap = Number(state.maxReplicas);
    if (!Number.isInteger(cap) || cap < state.quantity || cap > 1000)
      return {
        ready: false,
        missing: [],
        reason: "Cap must be a whole number from the quantity to 1000.",
      };
  }
  return { ready: true, missing: [] };
}

export const list = (text: string): string[] =>
  text
    .split(/[,\n]/)
    .map((x) => x.trim())
    .filter(Boolean);

export function parseEnv(text: string): Record<string, string> {
  return Object.fromEntries(
    text
      .split("\n")
      .map((line) => line.trim())
      .filter((line) => line.includes("="))
      .map((line) => [line.slice(0, line.indexOf("=")), line.slice(line.indexOf("=") + 1)]),
  );
}

/** The request: template, node and the user's choices. Image, runtime and
 * options are never sent; advanced fields only if the user changed them. */
export function buildRequest(catalog: TemplateCatalog, state: GuidedState): CreateWorkerRequest {
  const advanced = {
    ...(state.capabilities !== null && { capabilities: list(state.capabilities) }),
    ...(state.env !== null && { env: parseEnv(state.env) }),
    ...(state.extraHosts !== null && { extra_hosts: list(state.extraHosts) }),
    ...(state.maxReplicas !== null && { max_replicas: Number(state.maxReplicas) }),
  };
  const images = offeredImages(catalog, state);
  return {
    template: state.template,
    node: state.node,
    name: state.name,
    quantity: state.quantity,
    ...(state.image && images.length > 0 && { image: state.image }),
    ...(Object.keys(advanced).length > 0 && { advanced }),
  };
}

/** One short line for why a chosen template cannot be created: the first unmet
 * prerequisite, not the whole list (that lives under "Setup details"). */
export function blockingReason(readiness: ReturnType<typeof readinessFor>): string | undefined {
  if (!readiness || readiness.ready) return undefined;
  const unmet = readiness.checks.filter((c) => c.ok !== true);
  const first = unmet[0]?.detail ?? readiness.missing[0] ?? "Not ready on this node.";
  const short = first.length > 110 ? `${first.slice(0, 107)}…` : first;
  return unmet.length > 1 ? `${short} (+${unmet.length - 1} more)` : short;
}
