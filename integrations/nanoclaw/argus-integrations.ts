/**
 * Task Kubernetes integrations (integrations spec 3.6): the channel (the host
 * process, which holds the worker token) fetches each credential the task opted into
 * for the attempt it holds and installs one ephemeral, namespace-pinned kubeconfig in
 * an attempt-local directory the session container mounts. The model is told the path,
 * never the token; nothing global (no ~/.kube, no process-wide KUBECONFIG) changes.
 *
 * Each cluster is reached only through the Argus Kubernetes route
 * (argus-integrations-route.ts): the kubeconfig names it as the cluster's `proxy-url`
 * with a grant for this attempt, and the process-wide gateway proxy stays as it is for
 * everything else.
 *
 * A token never reaches a log line, an error, a block reason or the prompt: only
 * integration names, namespaces, versions and reason codes do. Removing the file and
 * revoking the grants stops this attempt from using the token; it does not revoke the
 * token upstream.
 */
import { randomUUID } from "node:crypto";
import { constants as fsc } from "node:fs";
import { type FileHandle, lstat, mkdir, open, readdir, rename, rm, stat } from "node:fs/promises";
import path from "node:path";

/** Below the agent group directory; the session container sees it under /workspace/agent. */
export const TASK_INTEGRATIONS_DIR = "task-integrations";
export const KUBECONFIG_FILE = "kubeconfig";
const CONTAINER_GROUP_DIR = "/workspace/agent";
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
// DNS-1123 label, as the API validates it; anything else is not installed.
const NAMESPACE = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;
const NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$/;
const BASE64 = /^[A-Za-z0-9+/]+={0,2}$/;
const MAX_CHANGED = 3; // re-reads after a rotation raced the fetch
/** Providers whose sessions the attempt's kubeconfig can be confined to (spec 3.6). */
const SUPPORTED_PROVIDERS = new Set(["anthropic"]);

export type IntegrationFailureReason =
  | "env_integration_unavailable"
  | "env_integration_unsupported"
  | "env_integration_invalid"
  | "env_integration_gateway_intercepts"
  | "env_integration_route_unavailable";

export type Readiness =
  | "ready"
  | "revoked"
  | "expired"
  | "unvalidated"
  | "secret_missing"
  | "store_unavailable";

/** `GET /tasks/{id}/integrations/status`: non-secret. */
export interface IntegrationState {
  integration_id: string;
  name: string;
  namespace: string;
  secret_version: number;
  /** Raised by an admin's config edit: reinstall, as for a new secret_version. */
  config_version: number;
  expires_at: string | null;
  readiness: Readiness;
}

/** `POST /tasks/{id}/integrations/{iid}/credential`: the only shape with a token. */
export interface KubernetesCredential {
  integration_id: string;
  name: string;
  server: string;
  namespace: string;
  ca_data: string | null;
  access: "read_only" | "read_write";
  token: string;
  secret_version: number;
  config_version: number;
  expires_at: string | null;
}

/** A cluster the kubeconfig would name: what the route grants and probes. Non-secret. */
export interface IntegrationRoute {
  integration_id: string;
  name: string;
  server: string;
  ca_data: string | null;
  /** Asked by every new tunnel: does Argus still deliver this to the attempt, at this version? */
  authorize: () => Promise<boolean>;
}

export type RouteResult =
  | { ok: true; proxies: Record<string, string> }
  | { ok: false; reason: IntegrationFailureReason; error: string };

/**
 * The Kubernetes route (argus-integrations-route.ts). `open` replaces every grant of the
 * task with one per route for this attempt and proves each end to end; its proxy URLs
 * carry the grant secrets. `revoke` drops the task's grants (only `attempt`'s, if given)
 * and closes their tunnels.
 */
export interface IntegrationTransport {
  open(
    who: { taskId: string; attempt: number },
    routes: readonly IntegrationRoute[],
  ): Promise<RouteResult>;
  revoke(taskId: string, attempt?: number): void;
}

export interface IntegrationsSource {
  status(taskId: string, attempt: number): Promise<IntegrationState[]>;
  credential(
    taskId: string,
    integrationId: string,
    attempt: number,
    version: number,
  ): Promise<KubernetesCredential>;
}

/** What the attempt has installed; non-secret, safe to log and to tell the model. */
export interface InstalledIntegrations {
  taskId: string;
  attempt: number;
  /** Host path of the attempt directory, and the kubeconfig path the session sees. */
  dir: string;
  kubeconfig: string;
  integrations: Array<Omit<IntegrationState, "readiness"> & { access: string }>;
}

export type IntegrationsResult =
  | { ok: true; installed: InstalledIntegrations | null }
  | { ok: false; reason: IntegrationFailureReason; error: string };

// No parameter properties: the kubectl e2e test loads this file with node's type stripping.
class IntegrationError extends Error {
  readonly reason: IntegrationFailureReason;
  constructor(reason: IntegrationFailureReason, message: string) {
    super(message);
    this.reason = reason;
  }
}

const code = (err: unknown) => (err as NodeJS.ErrnoException | null)?.code;
const apiCode = (err: unknown) => {
  const c = (err as { code?: unknown } | null)?.code;
  return typeof c === "string" ? c : null;
};
const apiStatus = (err: unknown) => {
  const s = (err as { status?: unknown } | null)?.status;
  return typeof s === "number" ? s : null;
};
const myUid = () => (typeof process.getuid === "function" ? process.getuid() : null);

/** API refusals that mean "this worker may not have it", never "try again". */
const INVALID_CODES = new Set(["integration_not_selected", "config_invalid"]);

function failure(err: unknown): IntegrationError {
  if (err instanceof IntegrationError) return err;
  // A local filesystem error: its code only (its message names paths).
  if (typeof (err as NodeJS.ErrnoException | null)?.syscall === "string")
    return new IntegrationError(
      "env_integration_invalid",
      `could not install the kubeconfig: ${code(err) ?? "error"}`,
    );
  const c = apiCode(err);
  if (c === "integration_engine_unsupported")
    return new IntegrationError("env_integration_unsupported", `Argus refused: ${c}`);
  if (c && INVALID_CODES.has(c))
    return new IntegrationError("env_integration_invalid", `Argus refused: ${c}`);
  // The API's code is safe to repeat (a fixed identifier); its message and anything
  // else are not, so only the code or the HTTP status is kept.
  const status = apiStatus(err);
  const what = c
    ? `Argus refused: ${c}`
    : status
      ? `Argus answered ${status}`
      : "Argus unreachable";
  return new IntegrationError("env_integration_unavailable", what);
}

export function taskIntegrationsPath(groupDir: string, taskId: string): string {
  if (!UUID.test(taskId)) throw new IntegrationError("env_integration_invalid", "bad task id");
  return path.join(groupDir, TASK_INTEGRATIONS_DIR, taskId);
}

interface Dir {
  fh: FileHandle;
  dev: number;
  ino: number;
}

/** `/proc/self/fd/<fd>/<leaf>`: only the leaf is resolved, inside the held directory. */
const at = (d: Dir, leaf: string) => `/proc/self/fd/${d.fh.fd}/${leaf}`;

/** Open `p` as a directory without following a final link; refuse if it cannot be pinned. */
async function holdDir(p: string, what: string): Promise<Dir & { uid: number; mode: number }> {
  let fh: FileHandle;
  try {
    fh = await open(p, fsc.O_RDONLY | fsc.O_DIRECTORY | fsc.O_NOFOLLOW);
  } catch (err) {
    if (code(err) === "ELOOP" || code(err) === "ENOTDIR")
      throw new IntegrationError("env_integration_invalid", `the ${what} is not a real directory`);
    throw err;
  }
  try {
    const st = await fh.stat();
    const pinned = await stat(`/proc/self/fd/${fh.fd}`).catch(() => null);
    if (!pinned || pinned.dev !== st.dev || pinned.ino !== st.ino)
      throw new IntegrationError("env_integration_invalid", `the ${what} cannot be held open`);
    return { fh, dev: st.dev, ino: st.ino, uid: st.uid, mode: st.mode };
  } catch (err) {
    await fh.close();
    throw err;
  }
}

/** A private real directory `name` in `parent`: created 0700, never a link, never shared. */
async function privateDir(parent: Dir, name: string, what: string, create = true): Promise<Dir> {
  if (create)
    try {
      await mkdir(at(parent, name), { mode: 0o700 });
    } catch (err) {
      if (code(err) !== "EEXIST") throw err;
    }
  const d = await holdDir(at(parent, name), what);
  const uid = myUid();
  const why =
    uid !== null && d.uid !== uid
      ? `the ${what} is not owned by the channel user`
      : (d.mode & 0o077) !== 0
        ? `the ${what} is not private`
        : null;
  if (why) {
    await d.fh.close();
    throw new IntegrationError("env_integration_invalid", why);
  }
  return d;
}

/** Write `data` to `leaf` through a fresh exclusive sibling, fsync and a rename. */
async function writeAtomic(d: Dir, leaf: string, data: string): Promise<void> {
  const tmp = `.${leaf}.${randomUUID()}.tmp`;
  const fh = await open(
    at(d, tmp),
    fsc.O_WRONLY | fsc.O_CREAT | fsc.O_EXCL | fsc.O_NOFOLLOW,
    0o600,
  );
  try {
    await fh.writeFile(data);
    await fh.chmod(0o600);
    await fh.sync();
  } finally {
    await fh.close();
  }
  await rename(at(d, tmp), at(d, leaf));
}

/** Remove every entry of a held directory; `rm` never follows a link it removes. */
async function emptyDir(d: Dir, keep?: string): Promise<void> {
  for (const name of await readdir(at(d, "."))) {
    if (name !== keep) await rm(at(d, name), { recursive: true, force: true });
  }
}

async function withDirs<T>(
  groupDir: string,
  taskId: string | null,
  create: boolean,
  fn: (root: Dir, task: Dir | null) => Promise<T>,
): Promise<T | null> {
  const held: Dir[] = [];
  try {
    held.push(await holdDir(groupDir, "group directory"));
    held.push(await privateDir(held[0], TASK_INTEGRATIONS_DIR, "task integrations root", create));
    if (taskId) held.push(await privateDir(held[1], taskId, "task integrations directory", create));
    return await fn(held[1], held[2] ?? null);
  } catch (err) {
    if (!create && code(err) === "ENOENT") return null;
    throw err;
  } finally {
    for (const d of held) await d.fh.close();
  }
}

function checkCredential(c: KubernetesCredential, want: IntegrationState): void {
  const bad = (why: string) => {
    throw new IntegrationError("env_integration_invalid", `integration ${want.name}: ${why}`);
  };
  if (c.integration_id !== want.integration_id || c.secret_version !== want.secret_version)
    bad("the delivered credential is not the one selected");
  if (!NAME.test(c.name)) bad("unsafe name");
  if (!NAMESPACE.test(c.namespace)) bad("namespace is not a DNS-1123 label");
  let server: URL | null = null;
  try {
    server = new URL(c.server);
  } catch {}
  if (!server || server.protocol !== "https:" || server.username || server.password)
    bad("server is not a plain https URL");
  if (c.ca_data !== null && (typeof c.ca_data !== "string" || !BASE64.test(c.ca_data)))
    bad("ca_data is not base64");
  if (typeof c.token !== "string" || !c.token || /[\s]/.test(c.token)) bad("unusable token");
}

/**
 * One context per integration, pinned to its namespace; the first is current. TLS is
 * verified against `ca_data` when given, else the system store; never skipped. Each
 * cluster goes through its route grant (`proxy-url`), never the process-wide proxy.
 */
export function renderKubeconfig(
  creds: readonly KubernetesCredential[],
  proxies: Readonly<Record<string, string>>,
): string {
  if (creds.some((c) => !proxies[c.integration_id]))
    throw new IntegrationError("env_integration_route_unavailable", "a cluster has no route");
  return `${JSON.stringify(
    {
      apiVersion: "v1",
      kind: "Config",
      preferences: {},
      clusters: creds.map((c) => ({
        name: c.name,
        cluster: {
          server: c.server,
          ...(c.ca_data ? { "certificate-authority-data": c.ca_data } : {}),
          "proxy-url": proxies[c.integration_id],
        },
      })),
      users: creds.map((c) => ({ name: c.name, user: { token: c.token } })),
      contexts: creds.map((c) => ({
        name: c.name,
        context: { cluster: c.name, user: c.name, namespace: c.namespace },
      })),
      "current-context": creds[0]?.name ?? "",
    },
    null,
    2,
  )}\n`;
}

/**
 * Grant this attempt a route to every cluster and prove each end to end, or refuse. Each
 * grant asks Argus again, for every new tunnel, whether the attempt still holds that
 * integration at that version (the status call checks the attempt's lease). No transport
 * at all fails closed: production wiring always supplies one.
 */
async function openRoutes(
  transport: IntegrationTransport | undefined,
  source: IntegrationsSource,
  creds: readonly KubernetesCredential[],
  who: { taskId: string; attempt: number },
): Promise<Record<string, string>> {
  if (!transport)
    throw new IntegrationError(
      "env_integration_route_unavailable",
      "no Kubernetes route is configured",
    );
  const holds = (c: KubernetesCredential) => async () =>
    (await source.status(who.taskId, who.attempt)).some(
      (s) =>
        s.integration_id === c.integration_id &&
        s.readiness === "ready" &&
        s.secret_version === c.secret_version &&
        s.config_version === c.config_version,
    );
  const opened = await transport.open(
    who,
    creds.map((c) => ({
      integration_id: c.integration_id,
      name: c.name,
      server: c.server,
      ca_data: c.ca_data,
      authorize: holds(c),
    })),
  );
  if (!opened.ok) throw new IntegrationError(opened.reason, opened.error);
  return opened.proxies;
}

/** Every selected integration at its current version, or a typed refusal. */
async function fetchAll(
  source: IntegrationsSource,
  taskId: string,
  attempt: number,
): Promise<{ states: IntegrationState[]; creds: KubernetesCredential[] }> {
  for (let round = 0; ; round++) {
    const states = await source.status(taskId, attempt);
    const blocked = states.filter((s) => s.readiness !== "ready");
    if (blocked.length)
      throw new IntegrationError(
        "env_integration_unavailable",
        `not deliverable: ${blocked.map((s) => `${s.name} (${s.readiness})`).join(", ")}`,
      );
    try {
      const creds: KubernetesCredential[] = [];
      for (const s of states) {
        const c = await source.credential(taskId, s.integration_id, attempt, s.secret_version);
        // A config edit landed after the status read: read it again, as for a rotation.
        if (c.config_version !== s.config_version) throw { code: "integration_changed" };
        checkCredential(c, s);
        creds.push(c);
      }
      return { states, creds };
    } catch (err) {
      // A rotation landed between the status read and the fetch: read it again.
      if (apiCode(err) !== "integration_changed" || round + 1 >= MAX_CHANGED) throw err;
    }
  }
}

function describe(
  taskId: string,
  attempt: number,
  dir: string,
  leaf: string,
  creds: readonly KubernetesCredential[],
): InstalledIntegrations {
  return {
    taskId,
    attempt,
    dir,
    kubeconfig: path.posix.join(
      CONTAINER_GROUP_DIR,
      TASK_INTEGRATIONS_DIR,
      taskId,
      leaf,
      KUBECONFIG_FILE,
    ),
    integrations: creds.map((c) => ({
      integration_id: c.integration_id,
      name: c.name,
      namespace: c.namespace,
      secret_version: c.secret_version,
      config_version: c.config_version,
      expires_at: c.expires_at,
      access: c.access,
    })),
  };
}

/**
 * Before the session starts: install the attempt's kubeconfig, or say why it cannot be.
 * A task with no integrations installs nothing; any previous attempt's directory is
 * removed either way, so attempts never share one.
 */
export async function installTaskIntegrations(opts: {
  groupDir: string;
  taskId: string;
  attempt: number;
  selected: readonly string[] | undefined;
  engineProvider: string | null;
  executorProvider: string | null;
  source: IntegrationsSource;
  /** The Kubernetes route; absent, the install fails closed. */
  transport?: IntegrationTransport;
}): Promise<IntegrationsResult> {
  const { groupDir, taskId, attempt, source } = opts;
  try {
    if (!opts.selected?.length) {
      if (UUID.test(taskId)) await removeTaskIntegrations(groupDir, taskId);
      return { ok: true, installed: null };
    }
    taskIntegrationsPath(groupDir, taskId);
    for (const [role, provider] of [
      ["engine", opts.engineProvider],
      ["executor", opts.executorProvider],
    ] as const) {
      if (role === "executor" && provider === null) continue;
      if (!provider || !SUPPORTED_PROVIDERS.has(provider))
        throw new IntegrationError(
          "env_integration_unsupported",
          `Kubernetes integrations need a Claude ${role}; this worker's is ${provider ?? "unknown"}`,
        );
    }
    const { creds } = await fetchAll(source, taskId, attempt);
    if (!creds.length)
      throw new IntegrationError(
        "env_integration_unavailable",
        "none of the selected integrations is deliverable to this task",
      );
    // Before anything is written: a cluster with no proven route never gets a kubeconfig.
    const proxies = await openRoutes(opts.transport, source, creds, { taskId, attempt });
    const leaf = `${attempt}-${randomUUID()}`;
    await withDirs(groupDir, taskId, true, async (_root, task) => {
      await emptyDir(task!);
      await mkdir(at(task!, leaf), { mode: 0o700 });
      const d = await privateDir(task!, leaf, "attempt directory", false);
      try {
        await writeAtomic(d, KUBECONFIG_FILE, renderKubeconfig(creds, proxies));
      } finally {
        await d.fh.close();
      }
    });
    const dir = path.join(taskIntegrationsPath(groupDir, taskId), leaf);
    return { ok: true, installed: describe(taskId, attempt, dir, leaf, creds) };
  } catch (err) {
    const e = failure(err);
    opts.transport?.revoke(taskId);
    if (UUID.test(taskId)) await removeTaskIntegrations(groupDir, taskId).catch(() => undefined);
    return { ok: false, reason: e.reason, error: e.message };
  }
}

/**
 * On every heartbeat while the attempt runs: anything no longer deliverable (revoked,
 * expired, the store down, the selection or the lease gone) removes the file; a rotation
 * replaces it atomically. A transient failure to reach Argus keeps it: the lease ends
 * the attempt, and removes it, if that persists (new tunnels already fail closed: each
 * asks Argus). Removal revokes the grants and closes their tunnels; a rotation re-grants
 * (closing the old version's tunnels) and probes again before it replaces the file.
 */
export async function recheckTaskIntegrations(
  groupDir: string,
  installed: InstalledIntegrations,
  source: IntegrationsSource,
  transport?: IntegrationTransport,
): Promise<{
  state: "kept" | "refreshed" | "removed";
  installed: InstalledIntegrations | null;
  why?: string;
}> {
  const { taskId, attempt } = installed;
  let states: IntegrationState[];
  try {
    states = await source.status(taskId, attempt);
  } catch (err) {
    const s = apiStatus(err);
    if (s !== null && s >= 400 && s < 500 && s !== 408 && s !== 429) {
      transport?.revoke(taskId);
      await removeTaskIntegrations(groupDir, taskId);
      return { state: "removed", installed: null, why: apiCode(err) ?? `HTTP ${s}` };
    }
    return { state: "kept", installed };
  }
  const had = new Map(installed.integrations.map((i) => [i.integration_id, i]));
  const gone = states.filter((s) => s.readiness !== "ready" || !had.has(s.integration_id));
  if (gone.length || states.length !== had.size) {
    transport?.revoke(taskId);
    await removeTaskIntegrations(groupDir, taskId);
    const why = gone.map((s) => `${s.name} (${s.readiness})`).join(", ") || "selection changed";
    return { state: "removed", installed: null, why };
  }
  const same = (s: IntegrationState) => {
    const i = had.get(s.integration_id);
    return i?.secret_version === s.secret_version && i.config_version === s.config_version;
  };
  if (states.every(same)) return { state: "kept", installed };
  try {
    const { creds } = await fetchAll(source, taskId, attempt);
    const proxies = await openRoutes(transport, source, creds, { taskId, attempt });
    const leaf = path.basename(installed.dir);
    const wrote = await withDirs(groupDir, taskId, false, async (_root, task) => {
      const d = await privateDir(task!, leaf, "attempt directory", false);
      try {
        await writeAtomic(d, KUBECONFIG_FILE, renderKubeconfig(creds, proxies));
      } finally {
        await d.fh.close();
      }
      return true;
    });
    if (!wrote) throw new IntegrationError("env_integration_invalid", "attempt directory gone");
    return { state: "refreshed", installed: describe(taskId, attempt, installed.dir, leaf, creds) };
  } catch (err) {
    transport?.revoke(taskId);
    await removeTaskIntegrations(groupDir, taskId);
    return { state: "removed", installed: null, why: failure(err).message };
  }
}

/** Remove the task's integration directory (every attempt's). Idempotent. */
export async function removeTaskIntegrations(groupDir: string, taskId: string): Promise<void> {
  taskIntegrationsPath(groupDir, taskId);
  await withDirs(groupDir, null, false, async (root) => {
    await rm(at(root, taskId), { recursive: true, force: true });
  });
}

/** At channel start: no attempt runs yet, so nothing installed before may survive. */
export async function sweepTaskIntegrations(groupDir: string): Promise<void> {
  const st = await lstat(path.join(groupDir, TASK_INTEGRATIONS_DIR)).catch(() => null);
  if (!st) return;
  if (!st.isDirectory()) {
    await rm(path.join(groupDir, TASK_INTEGRATIONS_DIR), { force: true });
    return;
  }
  await withDirs(groupDir, null, false, (root) => emptyDir(root));
}

/** The prompt's non-secret hint: where the kubeconfig is and how to use it. */
export function integrationLines(installed: InstalledIntegrations): string[] {
  const k = installed.kubeconfig;
  return [
    "Kubernetes access (this attempt only):",
    ...installed.integrations.map(
      (i) =>
        `- ${i.name}: namespace ${i.namespace}, ${i.access === "read_only" ? "read-only" : "read-write"}` +
        `${i.expires_at ? `, credential expires ${i.expires_at}` : ""}`,
    ),
    `Run kubectl as \`kubectl --kubeconfig ${k} ...\` (or \`KUBECONFIG=${k} kubectl ...\`); delegated`,
    "Claude runs get it as KUBECONFIG. Each context is pinned to its namespace; use `--context <name>`",
    "to pick another. Never print, copy or commit the kubeconfig (`cat`, `kubectl config view --raw`):",
    "it holds a live token. It is removed when the attempt ends or the integration is revoked.",
    "Each cluster is reached through its own Argus route (the kubeconfig's proxy-url), with",
    "end-to-end TLS. A proxy, x509 or certificate error from kubectl is an environment problem,",
    "not one to work around: never edit the proxy-url, server or CA, pass --server,",
    "--insecure-skip-tls-verify or --certificate-authority, or change HTTPS_PROXY or NO_PROXY;",
    "stop and reply FAIL: with the kubectl error.",
  ];
}
