import { ApiError, type Pool, type TemplateCatalog } from "./api";

/** How the API shows a stored env value: values are write-only. */
export const REDACTED = "***";

const ENV_KEY = /^[A-Za-z_][A-Za-z0-9_]*$/;

/**
 * The `env` part of a pool PATCH from the editor's text: changed or new values
 * are set, removed keys become null, and a value left at *** is never sent (a
 * stored key keeps its value; for any other key `parseEnv` reports it). Undefined
 * when nothing changed.
 */
export function envPatch(
  current: Record<string, string>,
  edited: Record<string, string>,
): Record<string, string | null> | undefined {
  const patch: Record<string, string | null> = {};
  for (const [key, value] of Object.entries(edited)) {
    if (value !== REDACTED) patch[key] = value;
  }
  for (const key of Object.keys(current)) {
    if (!(key in edited)) patch[key] = null;
  }
  return Object.keys(patch).length > 0 ? patch : undefined;
}

/** The editor's text for a pool's env: one KEY=value per line (stored values show as ***). */
export function envText(env: Record<string, string>): string {
  return Object.entries(env)
    .map(([k, v]) => `${k}=${v}`)
    .join("\n");
}

/**
 * Parse the env text. Nothing is dropped silently: a line that is not KEY=value, a bad key, a
 * duplicate key, or a *** for a key with no stored value is an error naming its line.
 */
export function parseEnv(
  text: string,
  stored: Record<string, string>,
): { env: Record<string, string>; error?: string } {
  const env: Record<string, string> = {};
  const lines = text.split("\n");
  for (const [i, raw] of lines.entries()) {
    const line = raw.trim();
    if (!line) continue;
    const at = line.indexOf("=");
    const key = at < 0 ? line : line.slice(0, at);
    if (at < 0 || !ENV_KEY.test(key)) {
      return { env, error: `Line ${i + 1}: expected KEY=value with a valid variable name.` };
    }
    if (key in env) return { env, error: `Line ${i + 1}: ${key} is set twice.` };
    const value = line.slice(at + 1);
    if (value === REDACTED && !(key in stored)) {
      return {
        env,
        error: `Line ${i + 1}: *** only keeps a stored value; ${key} has none. Enter its value or remove the line.`,
      };
    }
    env[key] = value;
  }
  return { env };
}

/** Comma-separated list → trimmed non-empty entries. */
export function splitList(text: string): string[] {
  return text
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
}

// -- images --------------------------------------------------------------------------------

const COMPONENT = "[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*";
const DOMAIN =
  "[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?(?:\\.[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?)*(?::[0-9]+)?";
const IMAGE_REF = new RegExp(
  `^(?:${DOMAIN}/)?${COMPONENT}(?:/${COMPONENT})*(?::[\\w][\\w.-]{0,127})?(?:@sha256:[a-f0-9]{64})?$`,
);

/** Why a typed image reference cannot be right (undefined: it looks valid). */
export function imageRefError(value: string): string | undefined {
  const v = value.trim();
  if (!v) return "Enter an image reference.";
  if (v.length > 500) return "An image reference is at most 500 characters.";
  if (!IMAGE_REF.test(v)) {
    return "Not a valid image reference. Expected [registry/]name[:tag][@sha256:digest], lowercase, no spaces.";
  }
  return undefined;
}

export type ImageRole = "host" | "agent";

export interface ImageOption {
  value: string;
  /** Where this choice comes from and what was measured about it. */
  note: string;
}

/**
 * The choices for one image role, or the reason there are none. `manual-only` means no
 * trustworthy source exists for this runtime/target: the UI says so and offers typing only.
 */
export type ImageChoices =
  | { kind: "options"; options: ImageOption[] }
  | { kind: "empty"; reason: string }
  | { kind: "manual-only"; reason: string };

const presence = (present: boolean | null | undefined): string =>
  present === true
    ? "present on the node"
    : present === false
      ? "not found on the node"
      : "presence not checked";

/**
 * Host and agent image choices from the template catalog, for one runtime and target. Host
 * and agent images are different roles with different sources, never merged:
 * - nanoclaw host: the operator's ARGUS_NANOCLAW_HOST_IMAGE; nanoclaw agent: ARGUS_NANOCLAW_AGENT_IMAGE;
 * - plain host (the worker image): the images the catalog verified as Argus workers on the node;
 * - the kubernetes target has no image inventory at all.
 */
export function imageChoices(
  catalog: TemplateCatalog,
  role: ImageRole,
  where: { runtime: "plain" | "nanoclaw"; target: "docker" | "kubernetes"; dockerHost: string },
): ImageChoices {
  if (where.target === "kubernetes") {
    return {
      kind: "manual-only",
      reason: "Argus does not list images in a Kubernetes cluster.",
    };
  }
  const node = where.dockerHost.trim() || catalog.default_node;
  const option = catalog.nodes.find((n) => n.id === node);
  if (!option) {
    return {
      kind: "manual-only",
      reason: `${node} is not a configured Docker node, so its images are not known.`,
    };
  }
  if (!option.available) {
    return {
      kind: "empty",
      reason: `The Docker node ${option.label} is unreachable${option.error ? `: ${option.error}` : ""}.`,
    };
  }
  const seen = new Map<string, ImageOption>();
  const add = (value: string, note: string) => {
    if (!seen.has(value)) seen.set(value, { value, note });
  };
  for (const t of catalog.templates) {
    if (t.runtime !== where.runtime) continue;
    const r = t.readiness.find((x) => x.node === node);
    if (!r) continue;
    if (where.runtime === "nanoclaw") {
      const img = role === "host" ? r.host_image : r.agent_image;
      if (img) {
        add(img.image, `configured in ${img.setting}; ${presence(img.present)}`);
      }
    } else if (role === "host") {
      for (const img of r.images) {
        add(img.image, `Argus worker image (${img.source}); present on the node`);
      }
    }
  }
  if (where.runtime === "plain" && role === "agent") {
    return { kind: "manual-only", reason: "Only NanoClaw pools have an agent image." };
  }
  if (seen.size === 0) {
    return {
      kind: "empty",
      reason:
        where.runtime === "nanoclaw"
          ? `No ${role} image is configured for NanoClaw on this node.`
          : "No Argus worker image was found on this node.",
    };
  }
  return { kind: "options", options: [...seen.values()] };
}

/** The image the catalog names for the OTHER role, to catch a host/agent mix-up. */
export function otherRoleImages(
  catalog: TemplateCatalog | undefined,
  role: ImageRole,
): Set<string> {
  const out = new Set<string>();
  for (const t of catalog?.templates ?? []) {
    if (t.runtime !== "nanoclaw") continue;
    for (const r of t.readiness) {
      const img = role === "host" ? r.agent_image : r.host_image;
      if (img) out.add(img.image);
    }
  }
  return out;
}

// -- form ----------------------------------------------------------------------------------

export interface PoolForm {
  name: string;
  image: string;
  agentImage: string;
  capabilities: string;
  desired: string;
  max: string;
  agentsPerInstance: string;
  cpus: string;
  memoryMb: string;
  allowMutableImages: boolean;
  env: string;
  extraHosts: string;
  dockerHost: string;
  dockerNetwork: string;
  target: "docker" | "kubernetes";
  k8sContext: string;
  k8sNamespace: string;
}

export const FORM_FIELDS = [
  "name",
  "image",
  "agentImage",
  "capabilities",
  "desired",
  "max",
  "agentsPerInstance",
  "cpus",
  "memoryMb",
  "allowMutableImages",
  "env",
  "extraHosts",
  "dockerHost",
  "dockerNetwork",
  "target",
  "k8sContext",
  "k8sNamespace",
] as const satisfies readonly (keyof PoolForm)[];

export type FieldErrors = Partial<Record<keyof PoolForm | "form", string>>;

const num = (v: number | null | undefined) => (v == null ? "" : String(v));

/** The form's text for a pool (or the defaults for a new one). */
export function toForm(pool?: Pool): PoolForm {
  return {
    name: pool?.name ?? "",
    image: pool?.image ?? "",
    agentImage: pool?.nanoclaw?.agent_image ?? "",
    capabilities: pool?.capabilities.join(", ") ?? "",
    desired: String(pool?.desired_replicas ?? 0),
    max: String(pool?.max_replicas ?? 3),
    agentsPerInstance: String(pool?.agents_per_instance ?? 1),
    cpus: num(pool?.cpus),
    memoryMb: num(pool?.memory_mb),
    allowMutableImages: pool?.allow_mutable_images ?? false,
    env: envText(pool?.env ?? {}),
    extraHosts: pool ? pool.docker.extra_hosts.join(", ") : "host.docker.internal:host-gateway",
    dockerHost: pool?.docker.host ?? "",
    dockerNetwork: pool?.docker.network ?? "",
    target: pool?.target ?? "docker",
    k8sContext: pool?.kubernetes?.context ?? "",
    k8sNamespace: pool?.kubernetes?.namespace ?? "default",
  };
}

function wholeNumber(text: string, label: string, min: number, max?: number): number | string {
  const n = Number(text);
  if (text.trim() === "" || !Number.isInteger(n) || n < min || (max !== undefined && n > max)) {
    return `${label} must be a whole number${max === undefined ? ` of at least ${min}` : ` from ${min} to ${max}`}.`;
  }
  return n;
}

export interface Built {
  /** PATCH body without expected_version (edit) or PUT body (create); undefined on errors. */
  body?: Record<string, unknown>;
  errors: FieldErrors;
  /** Edit only: nothing differs from the pool as opened. */
  unchanged?: boolean;
}

/**
 * Turn the draft into a request. Edit sends only what differs from `base` (the pool as the
 * editor opened it); nested options are merged over the stored ones, so the options the form
 * does not show (nanoclaw paths, runtime, kubernetes extras, …) travel unchanged. Env values
 * are write-only: *** is never sent. Image roles are checked: a changed host image must not be
 * the catalog's agent image, nor the reverse.
 */
export function buildPool(
  base: Pool | undefined,
  draft: PoolForm,
  catalog?: TemplateCatalog,
): Built {
  const errors: FieldErrors = {};
  const before = toForm(base);
  const changed = (f: keyof PoolForm) => !base || draft[f] !== before[f];
  const nanoclaw = base?.runtime === "nanoclaw";

  const desired = wholeNumber(draft.desired, "Desired replicas", 0);
  const max = wholeNumber(draft.max, "Max replicas", 0, 1000);
  const perInstance = wholeNumber(draft.agentsPerInstance, "Agents per instance", 1, 50);
  if (typeof desired === "string") errors.desired = desired;
  if (typeof max === "string") errors.max = max;
  if (typeof perInstance === "string") errors.agentsPerInstance = perInstance;
  if (typeof desired === "number" && typeof max === "number" && desired > max) {
    errors.desired = "Desired replicas cannot exceed the max replicas cap.";
  }
  let cpus: number | null = null;
  if (draft.cpus.trim()) {
    cpus = Number(draft.cpus);
    if (!(cpus > 0)) errors.cpus = "CPUs must be a number above 0, or empty for no limit.";
  }
  let memoryMb: number | null = null;
  if (draft.memoryMb.trim()) {
    memoryMb = Number(draft.memoryMb);
    if (!Number.isInteger(memoryMb) || memoryMb < 16) {
      errors.memoryMb = "Memory must be a whole number of MB, at least 16, or empty for no limit.";
    }
  }

  if (changed("image") || !base) {
    const why = imageRefError(draft.image);
    if (why) errors.image = why;
    else if (otherRoleImages(catalog, "host").has(draft.image.trim())) {
      errors.image = `${draft.image.trim()} is the NanoClaw agent image. The host image is the Docker image of the runtime host; set the agent image in its own field.`;
    }
  } else if (!draft.image.trim()) errors.image = "Enter an image reference.";
  if (nanoclaw && changed("agentImage")) {
    const why = imageRefError(draft.agentImage);
    if (why) errors.agentImage = why;
    else if (otherRoleImages(catalog, "agent").has(draft.agentImage.trim())) {
      errors.agentImage = `${draft.agentImage.trim()} is the NanoClaw host image. The agent image is what the host starts for each agent; set the host image in its own field.`;
    }
  }
  if (nanoclaw && draft.image.trim() && draft.image.trim() === draft.agentImage.trim()) {
    errors.agentImage = "The host image and the agent image are different images; these are equal.";
  }

  const parsed = parseEnv(draft.env, base?.env ?? {});
  if (parsed.error) errors.env = parsed.error;
  if (draft.target === "kubernetes" && !draft.k8sContext.trim()) {
    errors.k8sContext = "A kubeconfig context (or in-cluster) is required.";
  }
  if (base === undefined && !/^[a-z0-9][a-z0-9-]{0,39}$/.test(draft.name)) {
    errors.name = "Use lowercase letters, digits and dashes, up to 40 characters.";
  }
  if (Object.keys(errors).length > 0) return { errors };

  const docker = {
    ...(base?.docker ?? {}),
    host: draft.dockerHost.trim() || null,
    network: draft.dockerNetwork.trim() || null,
    extra_hosts: splitList(draft.extraHosts),
  };
  const kubernetes =
    draft.target === "kubernetes"
      ? {
          ...(base?.kubernetes ?? {}),
          context: draft.k8sContext.trim(),
          namespace: draft.k8sNamespace.trim(),
        }
      : undefined;
  const common = {
    image: draft.image.trim(),
    capabilities: splitList(draft.capabilities),
    desired_replicas: desired as number,
    max_replicas: max as number,
    agents_per_instance: perInstance as number,
    cpus,
    memory_mb: memoryMb,
    allow_mutable_images: draft.allowMutableImages,
  };

  if (!base) {
    return {
      errors,
      body: {
        ...common,
        target: draft.target,
        env: parsed.env,
        docker,
        ...(kubernetes && { kubernetes }),
      },
    };
  }

  const body: Record<string, unknown> = {};
  if (changed("image")) body.image = common.image;
  if (changed("capabilities")) body.capabilities = common.capabilities;
  if (changed("desired")) body.desired_replicas = common.desired_replicas;
  if (changed("max")) body.max_replicas = common.max_replicas;
  if (changed("agentsPerInstance")) body.agents_per_instance = common.agents_per_instance;
  if (changed("cpus")) body.cpus = common.cpus;
  if (changed("memoryMb")) body.memory_mb = common.memory_mb;
  if (changed("allowMutableImages")) body.allow_mutable_images = common.allow_mutable_images;
  if (changed("env")) {
    const env = envPatch(base.env, parsed.env);
    if (env) body.env = env;
  }
  if (
    base.target === "docker" &&
    (["extraHosts", "dockerHost", "dockerNetwork"] as const).some(changed)
  ) {
    body.docker = docker;
  }
  if (kubernetes && (changed("k8sContext") || changed("k8sNamespace"))) {
    body.kubernetes = kubernetes;
  }
  if (nanoclaw && base.nanoclaw && changed("agentImage")) {
    body.nanoclaw = { ...base.nanoclaw, agent_image: draft.agentImage.trim() };
  }
  return { errors, body, unchanged: Object.keys(body).length === 0 };
}

// -- server errors -------------------------------------------------------------------------

/** Server field path (`nanoclaw.agent_image`) → the form field it belongs to. */
const SERVER_FIELD: Record<string, keyof PoolForm> = {
  image: "image",
  "nanoclaw.agent_image": "agentImage",
  capabilities: "capabilities",
  desired_replicas: "desired",
  max_replicas: "max",
  agents_per_instance: "agentsPerInstance",
  cpus: "cpus",
  memory_mb: "memoryMb",
  allow_mutable_images: "allowMutableImages",
  env: "env",
  "docker.host": "dockerHost",
  "docker.network": "dockerNetwork",
  "docker.extra_hosts": "extraHosts",
  "kubernetes.context": "k8sContext",
  "kubernetes.namespace": "k8sNamespace",
};

/** Map an API refusal onto the form's fields; whatever has no field goes to `form`. */
export function poolErrors(err: unknown): FieldErrors {
  if (!(err instanceof ApiError)) return { form: (err as Error).message };
  const out: FieldErrors = {};
  const add = (field: keyof PoolForm | "form", why: string) => {
    out[field] = out[field] ? `${out[field]} ${why}` : why;
  };
  for (const d of err.details) {
    const at = d.indexOf(": ");
    // The path may carry an env key or list index (`env.TOKEN`, `docker.extra_hosts.0`).
    const path = (at < 0 ? "" : d.slice(0, at)).replace(/\.\d+$/, "");
    const field =
      SERVER_FIELD[path] ??
      SERVER_FIELD[path.split(".").slice(0, 2).join(".")] ??
      (path.startsWith("env.") ? "env" : undefined);
    if (field && at >= 0) add(field, d.slice(at + 2));
  }
  if (Object.keys(out).length === 0) add("form", err.message);
  return out;
}

export function isStale(err: unknown): err is ApiError {
  return err instanceof ApiError && err.code === "stale_version";
}
