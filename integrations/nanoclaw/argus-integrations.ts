/**
 * Task integrations (integrations spec 3.6): the channel (the host process, which holds
 * the worker token) fetches each credential the task opted into for the attempt it
 * holds.
 *
 * Kubernetes: one ephemeral, namespace-pinned kubeconfig in an attempt-local directory
 * the session container mounts. The model is told the path, never the token; nothing
 * global (no ~/.kube, no process-wide KUBECONFIG) changes. Each cluster is reached only
 * through the Argus Kubernetes route (argus-integrations-route.ts): the kubeconfig names
 * it as the cluster's `proxy-url` with a grant for this attempt, and the process-wide
 * gateway proxy stays as it is for everything else.
 *
 * MCP: the token stays in the channel's MCP broker (argus-integrations-mcp.ts); the
 * session's MCP config gets one `argus-integration-<name>` server per integration whose
 * URL is an attempt grant on the broker, which enforces the tool policy.
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
/** The prefix of the session MCP servers this module owns; nothing else is touched. */
export const MCP_SERVER_PREFIX = "argus-integration-";
const TOOL = /^[A-Za-z0-9_.\/-]{1,128}$/;

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
  | "tools_unknown"
  | "unvalidated"
  | "secret_missing"
  | "store_unavailable";

/** `GET /tasks/{id}/integrations/status`: non-secret. */
export interface IntegrationState {
  integration_id: string;
  kind: "kubernetes" | "mcp";
  name: string;
  /** Kubernetes only. */
  namespace: string | null;
  /** MCP only: `unknown` is never ready. */
  tool_policy: "allowlist" | "all" | "unknown" | null;
  secret_version: number;
  /** Raised by an admin's config edit: reinstall, as for a new secret_version. */
  config_version: number;
  expires_at: string | null;
  readiness: Readiness;
}

/** `POST /tasks/{id}/integrations/{iid}/credential`: with McpCredential, the only shapes with a token. */
export interface KubernetesCredential {
  kind: "kubernetes";
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

export interface McpCredential {
  kind: "mcp";
  integration_id: string;
  name: string;
  url: string;
  /** The allowlist; null with `all` (an admin's explicit read_write choice). */
  tools: string[] | null;
  tool_policy: "allowlist" | "all";
  access: "read_only" | "read_write";
  token: string;
  secret_version: number;
  config_version: number;
  expires_at: string | null;
}

export type IntegrationCredential = KubernetesCredential | McpCredential;

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

/** An MCP server the broker grants: holds the token, which never leaves the broker. */
export interface McpRoute {
  integration_id: string;
  name: string;
  url: string;
  tools: string[] | null;
  tool_policy: "allowlist" | "all";
  token: string;
  expires_at: string | null;
  /** Asked by every request: does Argus still deliver this to the attempt, at this version? */
  authorize: () => Promise<boolean>;
}

/** One entry of the session's MCP config: `http` is Streamable HTTP, `sse` legacy HTTP+SSE. */
export interface McpServerEntry {
  type: "http" | "sse";
  url: string;
}

export type McpOpenResult =
  | { ok: true; servers: Record<string, McpServerEntry> }
  | { ok: false; reason: IntegrationFailureReason; error: string };

/**
 * The MCP broker (argus-integrations-mcp.ts). `open` replaces the task's grants with one
 * per route for this attempt (a re-open of the same attempt keeps each server's URL) and
 * probes each server; its URLs carry the grant secrets. `renew` extends the attempt's
 * grants (each is short-lived), `revoke` drops them and cuts their requests and streams.
 */
export interface McpTransport {
  open(
    who: { taskId: string; attempt: number },
    routes: readonly McpRoute[],
  ): Promise<McpOpenResult>;
  renew(taskId: string, attempt: number): void;
  revoke(taskId: string, attempt?: number): void;
}

/**
 * The session's MCP config: `set` replaces every `argus-integration-*` server with
 * `servers` (keyed by server name) and keeps every other server; `{}` removes them.
 * Grant URLs are secrets: an implementation never logs them.
 */
export interface SessionMcp {
  set(servers: Record<string, McpServerEntry>): Promise<void>;
}

export interface IntegrationsSource {
  status(taskId: string, attempt: number): Promise<IntegrationState[]>;
  credential(
    taskId: string,
    integrationId: string,
    attempt: number,
    version: number,
  ): Promise<IntegrationCredential>;
}

/** What the attempt has installed; non-secret, safe to log and to tell the model. */
export interface InstalledIntegrations {
  taskId: string;
  attempt: number;
  /** Host path of the attempt directory, and the kubeconfig path the session sees (null: no Kubernetes). */
  dir: string;
  kubeconfig: string | null;
  integrations: Array<
    Omit<IntegrationState, "readiness"> & {
      access: string;
      /** MCP: the session's server name and the tools it may call (null: every listed tool). */
      server?: string;
      tools?: string[] | null;
    }
  >;
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

function checkCredential(c: IntegrationCredential, want: IntegrationState): void {
  const bad = (why: string) => {
    throw new IntegrationError("env_integration_invalid", `integration ${want.name}: ${why}`);
  };
  if (
    c.integration_id !== want.integration_id ||
    c.secret_version !== want.secret_version ||
    c.kind !== want.kind
  )
    bad("the delivered credential is not the one selected");
  if (!NAME.test(c.name)) bad("unsafe name");
  if (typeof c.token !== "string" || !c.token || /[\s]/.test(c.token)) bad("unusable token");
  if (c.kind === "mcp") {
    let url: URL | null = null;
    try {
      url = new URL(c.url);
    } catch {}
    if (!url || url.protocol !== "https:" || url.username || url.password || url.hash)
      bad("url is not a plain https URL");
    if (c.tool_policy !== "allowlist" && c.tool_policy !== "all") bad("no usable tool policy");
    if (c.tool_policy === "allowlist" && (!Array.isArray(c.tools) || !c.tools.length))
      bad("an allowlist with no tools");
    if (c.tools?.some((t) => typeof t !== "string" || !TOOL.test(t))) bad("unsafe tool name");
    return;
  }
  if (!NAMESPACE.test(c.namespace)) bad("namespace is not a DNS-1123 label");
  let server: URL | null = null;
  try {
    server = new URL(c.server);
  } catch {}
  if (!server || server.protocol !== "https:" || server.username || server.password)
    bad("server is not a plain https URL");
  if (c.ca_data !== null && (typeof c.ca_data !== "string" || !BASE64.test(c.ca_data)))
    bad("ca_data is not base64");
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

/** Does Argus still deliver `c` to the attempt, at its versions? (The status call checks the lease.) */
const holder =
  (source: IntegrationsSource, who: { taskId: string; attempt: number }) =>
  (c: IntegrationCredential) =>
  async () =>
    (await source.status(who.taskId, who.attempt)).some(
      (s) =>
        s.integration_id === c.integration_id &&
        s.readiness === "ready" &&
        s.secret_version === c.secret_version &&
        s.config_version === c.config_version,
    );

/** The session's MCP server name of an integration. */
export const mcpServerName = (name: string) => `${MCP_SERVER_PREFIX}${name}`;

/** `existing` MCP servers with every `argus-integration-*` one replaced by `servers`. */
export function withIntegrationServers(
  existing: unknown,
  servers: Record<string, McpServerEntry>,
): Record<string, unknown> {
  const kept =
    existing && typeof existing === "object" && !Array.isArray(existing)
      ? Object.entries(existing).filter(([k]) => !k.startsWith(MCP_SERVER_PREFIX))
      : [];
  return { ...Object.fromEntries(kept), ...servers };
}

/**
 * Grant this attempt a broker URL for every MCP server and probe each, then point the
 * session's MCP config at them; or refuse. No broker or no session config fails closed.
 */
async function openMcp(
  broker: McpTransport | undefined,
  session: SessionMcp | undefined,
  source: IntegrationsSource,
  creds: readonly McpCredential[],
  who: { taskId: string; attempt: number },
): Promise<void> {
  if (!broker || !session)
    throw new IntegrationError("env_integration_route_unavailable", "no MCP broker is configured");
  const holds = holder(source, who);
  const opened = await broker.open(
    who,
    creds.map((c) => ({
      integration_id: c.integration_id,
      name: c.name,
      url: c.url,
      tools: c.tools,
      tool_policy: c.tool_policy,
      token: c.token,
      expires_at: c.expires_at,
      authorize: holds(c),
    })),
  );
  if (!opened.ok) throw new IntegrationError(opened.reason, opened.error);
  const servers: Record<string, McpServerEntry> = {};
  for (const c of creds) {
    const entry = opened.servers[c.integration_id];
    if (!entry)
      throw new IntegrationError("env_integration_route_unavailable", `${c.name}: no grant`);
    servers[mcpServerName(c.name)] = entry;
  }
  await session.set(servers);
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
  if (!creds.length) return {};
  if (!transport)
    throw new IntegrationError(
      "env_integration_route_unavailable",
      "no Kubernetes route is configured",
    );
  const holds = holder(source, who);
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
): Promise<{ states: IntegrationState[]; creds: IntegrationCredential[] }> {
  for (let round = 0; ; round++) {
    const states = await source.status(taskId, attempt);
    const blocked = states.filter((s) => s.readiness !== "ready");
    if (blocked.length)
      throw new IntegrationError(
        "env_integration_unavailable",
        `not deliverable: ${blocked.map((s) => `${s.name} (${s.readiness})`).join(", ")}`,
      );
    try {
      const creds: IntegrationCredential[] = [];
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
  creds: readonly IntegrationCredential[],
): InstalledIntegrations {
  return {
    taskId,
    attempt,
    dir,
    kubeconfig: creds.some((c) => c.kind === "kubernetes")
      ? path.posix.join(CONTAINER_GROUP_DIR, TASK_INTEGRATIONS_DIR, taskId, leaf, KUBECONFIG_FILE)
      : null,
    integrations: creds.map((c) => ({
      integration_id: c.integration_id,
      kind: c.kind,
      name: c.name,
      namespace: c.kind === "kubernetes" ? c.namespace : null,
      tool_policy: c.kind === "mcp" ? c.tool_policy : null,
      secret_version: c.secret_version,
      config_version: c.config_version,
      expires_at: c.expires_at,
      access: c.access,
      ...(c.kind === "mcp" ? { server: mcpServerName(c.name), tools: c.tools } : {}),
    })),
  };
}

const kube = (creds: readonly IntegrationCredential[]) =>
  creds.filter((c): c is KubernetesCredential => c.kind === "kubernetes");
const mcp = (creds: readonly IntegrationCredential[]) =>
  creds.filter((c): c is McpCredential => c.kind === "mcp");

/** Cut every grant of the task and take its servers out of the session's MCP config. */
async function revokeAll(
  taskId: string,
  transport: IntegrationTransport | undefined,
  mcpBroker: McpTransport | undefined,
  session: SessionMcp | undefined,
): Promise<void> {
  transport?.revoke(taskId);
  mcpBroker?.revoke(taskId);
  await session?.set({});
}

export interface IntegrationTransports {
  /** The Kubernetes route; absent, a Kubernetes integration fails closed. */
  transport?: IntegrationTransport;
  /** The MCP broker and the session's MCP config; either absent, an MCP integration fails closed. */
  mcp?: McpTransport;
  sessionMcp?: SessionMcp;
}

/**
 * Before the session starts: install the attempt's kubeconfig and MCP servers, or say why they cannot be.
 * A task with no integrations installs nothing; any previous attempt's directory is
 * removed either way, so attempts never share one.
 */
export async function installTaskIntegrations(
  opts: {
    groupDir: string;
    taskId: string;
    attempt: number;
    selected: readonly string[] | undefined;
    engineProvider: string | null;
    executorProvider: string | null;
    source: IntegrationsSource;
  } & IntegrationTransports,
): Promise<IntegrationsResult> {
  const { groupDir, taskId, attempt, source } = opts;
  try {
    if (!opts.selected?.length) {
      if (UUID.test(taskId)) await removeTaskIntegrations(groupDir, taskId);
      await opts.sessionMcp?.set({});
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
          `Task integrations need a Claude ${role}; this worker's is ${provider ?? "unknown"}`,
        );
    }
    const { creds } = await fetchAll(source, taskId, attempt);
    if (!creds.length)
      throw new IntegrationError(
        "env_integration_unavailable",
        "none of the selected integrations is deliverable to this task",
      );
    // Before anything is written: a cluster with no proven route never gets a kubeconfig.
    const clusters = kube(creds);
    const proxies = await openRoutes(opts.transport, source, clusters, { taskId, attempt });
    const leaf = `${attempt}-${randomUUID()}`;
    await withDirs(groupDir, taskId, true, async (_root, task) => {
      await emptyDir(task!);
      await mkdir(at(task!, leaf), { mode: 0o700 });
      if (!clusters.length) return;
      const d = await privateDir(task!, leaf, "attempt directory", false);
      try {
        await writeAtomic(d, KUBECONFIG_FILE, renderKubeconfig(clusters, proxies));
      } finally {
        await d.fh.close();
      }
    });
    const servers = mcp(creds);
    if (servers.length)
      await openMcp(opts.mcp, opts.sessionMcp, source, servers, { taskId, attempt });
    else await opts.sessionMcp?.set({});
    const dir = path.join(taskIntegrationsPath(groupDir, taskId), leaf);
    return { ok: true, installed: describe(taskId, attempt, dir, leaf, creds) };
  } catch (err) {
    const e = failure(err);
    await revokeAll(taskId, opts.transport, opts.mcp, opts.sessionMcp).catch(() => undefined);
    if (UUID.test(taskId)) await removeTaskIntegrations(groupDir, taskId).catch(() => undefined);
    return { ok: false, reason: e.reason, error: e.message };
  }
}

/**
 * On every heartbeat while the attempt runs: anything no longer deliverable (revoked,
 * expired, the store down, the selection or the lease gone) removes the file and the MCP
 * servers; a rotation or config edit replaces them. A transient failure to reach Argus
 * keeps them: the lease ends the attempt, and removes them, if that persists (new tunnels
 * and MCP requests already fail closed: each asks Argus). Removal revokes the grants and
 * cuts their tunnels, requests and streams; a rotation re-grants (cutting the old
 * version's) and probes again before it replaces anything. A kept install renews the
 * attempt's MCP grants, which otherwise lapse within minutes.
 */
export async function recheckTaskIntegrations(
  groupDir: string,
  installed: InstalledIntegrations,
  source: IntegrationsSource,
  transports: IntegrationTransports = {},
): Promise<{
  state: "kept" | "refreshed" | "removed";
  installed: InstalledIntegrations | null;
  why?: string;
}> {
  const { taskId, attempt } = installed;
  const { transport } = transports;
  let states: IntegrationState[];
  try {
    states = await source.status(taskId, attempt);
  } catch (err) {
    const s = apiStatus(err);
    if (s !== null && s >= 400 && s < 500 && s !== 408 && s !== 429) {
      await revokeAll(taskId, transport, transports.mcp, transports.sessionMcp);
      await removeTaskIntegrations(groupDir, taskId);
      return { state: "removed", installed: null, why: apiCode(err) ?? `HTTP ${s}` };
    }
    return { state: "kept", installed };
  }
  const had = new Map(installed.integrations.map((i) => [i.integration_id, i]));
  const gone = states.filter((s) => s.readiness !== "ready" || !had.has(s.integration_id));
  if (gone.length || states.length !== had.size) {
    await revokeAll(taskId, transport, transports.mcp, transports.sessionMcp);
    await removeTaskIntegrations(groupDir, taskId);
    const why = gone.map((s) => `${s.name} (${s.readiness})`).join(", ") || "selection changed";
    return { state: "removed", installed: null, why };
  }
  const same = (s: IntegrationState) => {
    const i = had.get(s.integration_id);
    return i?.secret_version === s.secret_version && i.config_version === s.config_version;
  };
  if (states.every(same)) {
    transports.mcp?.renew(taskId, attempt);
    return { state: "kept", installed };
  }
  try {
    const { creds } = await fetchAll(source, taskId, attempt);
    const clusters = kube(creds);
    const proxies = await openRoutes(transport, source, clusters, { taskId, attempt });
    const leaf = path.basename(installed.dir);
    const wrote = await withDirs(groupDir, taskId, false, async (_root, task) => {
      const d = await privateDir(task!, leaf, "attempt directory", false);
      try {
        if (clusters.length)
          await writeAtomic(d, KUBECONFIG_FILE, renderKubeconfig(clusters, proxies));
      } finally {
        await d.fh.close();
      }
      return true;
    });
    if (!wrote) throw new IntegrationError("env_integration_invalid", "attempt directory gone");
    const servers = mcp(creds);
    if (servers.length)
      await openMcp(transports.mcp, transports.sessionMcp, source, servers, { taskId, attempt });
    return { state: "refreshed", installed: describe(taskId, attempt, installed.dir, leaf, creds) };
  } catch (err) {
    await revokeAll(taskId, transport, transports.mcp, transports.sessionMcp).catch(
      () => undefined,
    );
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

/** The prompt's non-secret hint: where the kubeconfig is, which MCP tools exist, how to use them. */
export function integrationLines(installed: InstalledIntegrations): string[] {
  const lines: string[] = [];
  const k = installed.kubeconfig;
  const clusters = installed.integrations.filter((i) => i.kind === "kubernetes");
  if (k && clusters.length)
    lines.push(
      "Kubernetes access (this attempt only):",
      ...clusters.map(
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
    );
  const servers = installed.integrations.filter((i) => i.kind === "mcp" && i.server);
  if (servers.length)
    lines.push(
      ...(lines.length ? [""] : []),
      "MCP integrations (this attempt only, through an Argus broker that holds the credential):",
      ...servers.map(
        (i) =>
          `- ${i.name}: MCP server \`${i.server}\`, ` +
          (i.tools
            ? `tools ${i.tools.map((t) => `\`mcp__${i.server}__${t}\``).join(", ")}`
            : "every tool it lists") +
          `${i.expires_at ? `; credential expires ${i.expires_at}` : ""}`,
      ),
      "Only these tools work, and only tool calls (no resources or prompts). A call Argus denies",
      '("denied by Argus policy") is final: do not retry it or reach the server another way.',
      "If the tools are missing or every call fails, stop and reply FAIL: with the error.",
    );
  return lines;
}
