import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import https from "node:https";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import tls from "node:tls";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import {
  type IntegrationRoute,
  type IntegrationState,
  type IntegrationsSource,
  type IntegrationTransport,
  installTaskIntegrations,
  integrationLines,
  KUBECONFIG_FILE,
  type KubernetesCredential,
  type McpCredential,
  type McpOpenResult,
  type McpRoute,
  type McpServerEntry,
  type McpTransport,
  type RouteResult,
  recheckTaskIntegrations,
  removeTaskIntegrations,
  renderKubeconfig,
  sweepTaskIntegrations,
  TASK_INTEGRATIONS_DIR,
  taskIntegrationsPath,
  withIntegrationServers,
} from "./argus-integrations.js";
import {
  createKubernetesRoute,
  internalAddress,
  type KubernetesRoute,
  nanoclawKubernetesRoute,
} from "./argus-integrations-route.js";

const SECRET = "fake-token-NEVER-IN-A-PROMPT";
const dirs: string[] = [];
afterEach(() => {
  for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
});

function group(): string {
  const d = mkdtempSync(path.join(os.tmpdir(), "argus-integrations-"));
  dirs.push(d);
  return d;
}

class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super(`${code}: the API message may echo ${SECRET}`);
  }
}

/** An in-memory Argus: one integration per entry, current version and readiness. */
function fakeArgus(
  entries: Array<
    Partial<IntegrationState> & {
      token?: string;
      ca_data?: string | null;
      tools?: string[] | null;
      url?: string;
    }
  > = [{}],
) {
  const rows = entries.map((e, i) => ({
    integration_id: e.integration_id ?? `int-${i}`,
    kind: e.kind ?? "kubernetes",
    name: e.name ?? `${e.kind === "mcp" ? "server" : "cluster"}-${i}`,
    namespace: e.kind === "mcp" ? null : (e.namespace ?? "team-a"),
    tool_policy:
      e.kind === "mcp" ? (e.tool_policy ?? (e.tools === null ? "all" : "allowlist")) : null,
    tools: e.tools === undefined ? ["search"] : e.tools,
    url: e.url ?? "https://mcp.example.test/mcp",
    secret_version: e.secret_version ?? 1,
    config_version: e.config_version ?? 1,
    expires_at: e.expires_at ?? "2026-12-01T00:00:00Z",
    readiness: e.readiness ?? "ready",
    token: e.token ?? `${SECRET}-${i}`,
    ca_data: e.ca_data === undefined ? Buffer.from("PEM").toString("base64") : e.ca_data,
  }));
  const calls: string[] = [];
  let statusError: unknown = null;
  let raceOnce = false;
  const source: IntegrationsSource = {
    status: async (taskId, attempt) => {
      calls.push(`status:${taskId}:${attempt}`);
      if (statusError) throw statusError;
      return rows.map(({ token: _t, ca_data: _c, tools: _l, url: _u, ...s }) => ({ ...s }));
    },
    credential: async (taskId, id, attempt, version) => {
      calls.push(`credential:${id}:${attempt}:${version}`);
      const r = rows.find((x) => x.integration_id === id)!;
      if (raceOnce) {
        raceOnce = false;
        r.secret_version += 1;
        r.token = `${SECRET}-rotated`;
      }
      if (version !== r.secret_version) throw new ApiError(409, "integration_changed");
      if (r.readiness !== "ready") throw new ApiError(409, `integration_${r.readiness}`);
      if (r.kind === "mcp")
        return {
          kind: "mcp",
          integration_id: r.integration_id,
          name: r.name,
          url: r.url,
          tools: r.tool_policy === "all" ? null : r.tools,
          tool_policy: r.tool_policy === "all" ? "all" : "allowlist",
          access: r.tool_policy === "all" ? "read_write" : "read_only",
          token: r.token,
          secret_version: r.secret_version,
          config_version: r.config_version,
          expires_at: r.expires_at,
        } satisfies McpCredential;
      return {
        kind: "kubernetes",
        integration_id: r.integration_id,
        name: r.name,
        server: "https://k8s.example.test:6443",
        namespace: r.namespace ?? "",
        ca_data: r.ca_data,
        access: "read_only",
        token: r.token,
        secret_version: r.secret_version,
        config_version: r.config_version,
        expires_at: r.expires_at,
      } satisfies KubernetesCredential;
    },
  };
  return {
    rows,
    calls,
    source,
    failStatus: (err: unknown) => {
      statusError = err;
    },
    raceNextFetch: () => {
      raceOnce = true;
    },
  };
}

const TASK = "0b5a3a6e-8f4c-4d0e-9a51-7c1f2b3d4e5f";
const grantUrl = (id: string) => `http://argus:grant-${id}@172.17.0.1:7000`;

/** A route that records what it is asked; it grants every route unless told otherwise. */
function fakeRoute(answer?: (routes: readonly IntegrationRoute[]) => RouteResult) {
  const opened: Array<{ who: { taskId: string; attempt: number }; routes: IntegrationRoute[] }> =
    [];
  const revoked: Array<[string, number | undefined]> = [];
  const transport: IntegrationTransport = {
    open: async (who, routes) => {
      opened.push({ who, routes: [...routes] });
      return answer
        ? answer(routes)
        : {
            ok: true,
            proxies: Object.fromEntries(
              routes.map((r) => [r.integration_id, grantUrl(r.integration_id)]),
            ),
          };
    },
    revoke: (taskId, attempt) => {
      revoked.push([taskId, attempt]);
    },
  };
  return { opened, revoked, transport };
}

function install(
  groupDir: string,
  source: IntegrationsSource,
  over: Partial<Parameters<typeof installTaskIntegrations>[0]> = {},
) {
  return installTaskIntegrations({
    groupDir,
    taskId: TASK,
    attempt: 1,
    selected: ["int-0"],
    engineProvider: "anthropic",
    executorProvider: null,
    source,
    transport: fakeRoute().transport,
    ...over,
  });
}

function hostPath(groupDir: string, containerPath: string | null): string {
  if (containerPath === null) throw new Error("no kubeconfig installed");
  return path.join(groupDir, path.relative("/workspace/agent", containerPath));
}

describe("installTaskIntegrations", () => {
  it("writes a private, namespace-pinned kubeconfig in a fresh attempt directory", async () => {
    const g = group();
    const argus = fakeArgus([{}, { namespace: "team-b", ca_data: null }]);
    const got = await install(g, argus.source, { selected: ["int-0", "int-1"] });
    expect(got.ok).toBe(true);
    if (!got.ok || !got.installed) throw new Error("not installed");
    const { installed } = got;
    expect(installed.kubeconfig).toMatch(
      new RegExp(`^/workspace/agent/${TASK_INTEGRATIONS_DIR}/${TASK}/1-[0-9a-f-]{36}/kubeconfig$`),
    );
    const file = hostPath(g, installed.kubeconfig);
    expect(statSync(file).mode & 0o777).toBe(0o600);
    expect(statSync(path.dirname(file)).mode & 0o777).toBe(0o700);
    expect(statSync(taskIntegrationsPath(g, TASK)).mode & 0o777).toBe(0o700);
    expect(statSync(path.join(g, TASK_INTEGRATIONS_DIR)).mode & 0o777).toBe(0o700);
    // Only the kubeconfig: no temp file left behind.
    expect(readdirSync(path.dirname(file))).toEqual([KUBECONFIG_FILE]);

    const cfg = JSON.parse(readFileSync(file, "utf8"));
    expect(cfg["current-context"]).toBe("cluster-0");
    expect(cfg.contexts.map((c: any) => c.context.namespace)).toEqual(["team-a", "team-b"]);
    expect(cfg.clusters[0].cluster["certificate-authority-data"]).toBeTruthy();
    // Each cluster through its own route grant; the process-wide proxy is not named.
    expect(cfg.clusters[0].cluster["proxy-url"]).toBe(grantUrl("int-0"));
    expect(cfg.clusters[1].cluster).toEqual({
      server: "https://k8s.example.test:6443",
      "proxy-url": grantUrl("int-1"),
    });
    expect(readFileSync(file, "utf8")).not.toMatch(/insecure/);
    expect(cfg.users[0].user.token).toBe(`${SECRET}-0`);
    expect(argus.calls).toEqual([
      `status:${TASK}:1`,
      "credential:int-0:1:1",
      "credential:int-1:1:1",
    ]);
  });

  it("tells the model the path, namespace and expiry, never the token", async () => {
    const g = group();
    const got = await install(g, fakeArgus().source);
    if (!got.ok || !got.installed) throw new Error("not installed");
    const text = integrationLines(got.installed).join("\n");
    expect(text).toContain(got.installed.kubeconfig);
    expect(text).toContain("namespace team-a");
    expect(text).toContain("2026-12-01T00:00:00Z");
    expect(text).toContain("kubectl config view --raw");
    expect(text).not.toContain(SECRET);
    expect(JSON.stringify(got.installed)).not.toContain(SECRET);
  });

  it("installs nothing for a task without integrations and clears a stale directory", async () => {
    const g = group();
    const argus = fakeArgus();
    const first = await install(g, argus.source);
    if (!first.ok || !first.installed) throw new Error("not installed");
    const got = await install(g, argus.source, { attempt: 2, selected: [] });
    expect(got).toEqual({ ok: true, installed: null });
    expect(existsSync(taskIntegrationsPath(g, TASK))).toBe(false);
  });

  it("gives a retry a distinct directory and removes the previous attempt's", async () => {
    const g = group();
    const argus = fakeArgus();
    const one = await install(g, argus.source);
    const two = await install(g, argus.source, { attempt: 2 });
    if (!one.ok || !one.installed || !two.ok || !two.installed) throw new Error("not installed");
    expect(two.installed.dir).not.toBe(one.installed.dir);
    expect(existsSync(one.installed.dir)).toBe(false);
    expect(readdirSync(taskIntegrationsPath(g, TASK))).toEqual([path.basename(two.installed.dir)]);
  });

  it.each([
    ["revoked", "env_integration_unavailable"],
    ["expired", "env_integration_unavailable"],
    ["unvalidated", "env_integration_unavailable"],
    ["secret_missing", "env_integration_unavailable"],
    ["store_unavailable", "env_integration_unavailable"],
  ] as const)("fails closed when an integration is %s", async (readiness, reason) => {
    const g = group();
    const argus = fakeArgus([{}, { readiness }]);
    const got = await install(g, argus.source, { selected: ["int-0", "int-1"] });
    expect(got).toMatchObject({ ok: false, reason });
    if (got.ok) throw new Error("installed");
    expect(got.error).toContain(`cluster-1 (${readiness})`);
    expect(argus.calls.filter((c) => c.startsWith("credential"))).toEqual([]);
    expect(existsSync(taskIntegrationsPath(g, TASK))).toBe(false);
  });

  it.each([
    [{ engineProvider: "openai" }, "engine"],
    [{ engineProvider: null }, "engine"],
    [{ executorProvider: "openai" }, "executor"],
  ])("refuses an engine it cannot confine (%o) before fetching", async (over, role) => {
    const g = group();
    const argus = fakeArgus();
    const got = await install(g, argus.source, over);
    expect(got).toMatchObject({ ok: false, reason: "env_integration_unsupported" });
    if (got.ok) throw new Error("installed");
    expect(got.error).toContain(`Claude ${role}`);
    expect(argus.calls).toEqual([]);
  });

  it("maps API refusals to reasons with the code only, never the message", async () => {
    for (const [status, code, reason] of [
      [403, "not_assignee", "env_integration_unavailable"],
      [409, "stale_attempt", "env_integration_unavailable"],
      [403, "integration_not_selected", "env_integration_invalid"],
      [409, "integration_engine_unsupported", "env_integration_unsupported"],
      [503, "secret_store_unavailable", "env_integration_unavailable"],
    ] as const) {
      const g = group();
      const argus = fakeArgus();
      argus.failStatus(new ApiError(status, code));
      const got = await install(g, argus.source);
      expect(got).toEqual({ ok: false, reason, error: `Argus refused: ${code}` });
    }
    const g = group();
    const argus = fakeArgus();
    argus.failStatus(new TypeError(`fetch failed for ${SECRET}`));
    expect(await install(g, argus.source)).toEqual({
      ok: false,
      reason: "env_integration_unavailable",
      error: "Argus unreachable",
    });
  });

  it("re-reads after a rotation races the fetch and installs the new version", async () => {
    const g = group();
    const argus = fakeArgus();
    argus.raceNextFetch();
    const got = await install(g, argus.source);
    if (!got.ok || !got.installed) throw new Error(JSON.stringify(got));
    expect(got.installed.integrations[0].secret_version).toBe(2);
    expect(readFileSync(hostPath(g, got.installed.kubeconfig), "utf8")).toContain(
      `${SECRET}-rotated`,
    );
  });

  it.each([
    ["server", { server: "http://k8s.example.test" }],
    ["server", { server: "https://user:pw@k8s.example.test" }],
    ["namespace", { namespace: "../etc" }],
    ["name", { name: "../../x" }],
    ["ca_data", { ca_data: "not base64!" }],
    ["token", { token: "a b" }],
    ["version", { secret_version: 7 }],
  ])("refuses a credential with an unsafe %s", async (_what, bad) => {
    const g = group();
    const argus = fakeArgus();
    const source: IntegrationsSource = {
      ...argus.source,
      credential: async (...a) => ({ ...(await argus.source.credential(...a)), ...bad }),
    };
    const got = await install(g, source);
    expect(got).toMatchObject({ ok: false, reason: "env_integration_invalid" });
    expect(JSON.stringify(got)).not.toContain(SECRET);
    expect(existsSync(taskIntegrationsPath(g, TASK))).toBe(false);
  });

  it("refuses a task id that is not a canonical UUID", async () => {
    const got = await install(group(), fakeArgus().source, { taskId: "../../escape" });
    expect(got).toMatchObject({ ok: false, reason: "env_integration_invalid" });
  });

  it("never writes through a symlinked root or task directory", async () => {
    for (const link of ["root", "task"] as const) {
      const g = group();
      const outside = group();
      if (link === "root") symlinkSync(outside, path.join(g, TASK_INTEGRATIONS_DIR));
      else {
        mkdirSync(path.join(g, TASK_INTEGRATIONS_DIR), { mode: 0o700 });
        symlinkSync(outside, taskIntegrationsPath(g, TASK));
      }
      const got = await install(g, fakeArgus().source);
      expect(got).toMatchObject({ ok: false, reason: "env_integration_invalid" });
      expect(readdirSync(outside)).toEqual([]);
    }
  });

  it("refuses a shared (group-readable) task directory", async () => {
    const g = group();
    mkdirSync(taskIntegrationsPath(g, TASK), { recursive: true, mode: 0o755 });
    const got = await install(g, fakeArgus().source);
    expect(got).toMatchObject({ ok: false, reason: "env_integration_invalid" });
  });

  it.each([
    ["env_integration_gateway_intercepts", "cluster-0: something intercepts TLS"],
    ["env_integration_route_unavailable", "cluster-0: the route refused the CONNECT (HTTP 502)"],
  ] as const)("blocks with the route's %s and leaves no kubeconfig", async (reason, error) => {
    const g = group();
    const argus = fakeArgus();
    const stale = await install(g, argus.source);
    if (!stale.ok || !stale.installed) throw new Error("not installed");
    const route = fakeRoute(() => ({ ok: false, reason, error }));
    const got = await install(g, argus.source, { attempt: 2, transport: route.transport });
    expect(got).toEqual({ ok: false, reason, error });
    expect(existsSync(taskIntegrationsPath(g, TASK))).toBe(false);
    expect(route.revoked).toEqual([[TASK, undefined]]);
    // The route sees where to go and whom it serves, never the token.
    expect(route.opened).toEqual([
      {
        who: { taskId: TASK, attempt: 2 },
        routes: [
          {
            integration_id: "int-0",
            name: "cluster-0",
            server: "https://k8s.example.test:6443",
            ca_data: Buffer.from("PEM").toString("base64"),
            authorize: expect.any(Function),
          },
        ],
      },
    ]);
    expect(JSON.stringify(route.opened)).not.toContain(SECRET);
  });

  it("fails closed without a route", async () => {
    const g = group();
    const got = await install(g, fakeArgus().source, { transport: undefined });
    expect(got).toEqual({
      ok: false,
      reason: "env_integration_route_unavailable",
      error: "no Kubernetes route is configured",
    });
    expect(existsSync(taskIntegrationsPath(g, TASK))).toBe(false);
  });

  it("authorizes each tunnel against Argus: this attempt, ready, the granted version", async () => {
    const g = group();
    const argus = fakeArgus([{}, {}]);
    const route = fakeRoute();
    const got = await install(g, argus.source, {
      attempt: 3,
      selected: ["int-0", "int-1"],
      transport: route.transport,
    });
    expect(got.ok).toBe(true);
    const [one, two] = route.opened[0]!.routes;
    argus.calls.splice(0);
    expect(await one!.authorize()).toBe(true);
    // The lease check is Argus's: the status read for exactly this task and attempt.
    expect(argus.calls).toEqual([`status:${TASK}:3`]);
    argus.rows[0]!.readiness = "revoked";
    expect(await one!.authorize()).toBe(false);
    expect(await two!.authorize()).toBe(true);
    argus.rows[1]!.config_version = 2;
    expect(await two!.authorize()).toBe(false);
    argus.rows[1]!.config_version = 1;
    argus.rows[1]!.secret_version = 2;
    expect(await two!.authorize()).toBe(false);
    argus.rows.splice(1, 1);
    expect(await two!.authorize()).toBe(false);
    argus.failStatus(new ApiError(409, "stale_attempt"));
    await expect(one!.authorize()).rejects.toThrow();
  });

  it("removes a planted symlink in the task directory instead of following it", async () => {
    const g = group();
    const outside = group();
    writeFileSync(path.join(outside, "keep"), "x");
    mkdirSync(taskIntegrationsPath(g, TASK), { recursive: true, mode: 0o700 });
    symlinkSync(outside, path.join(taskIntegrationsPath(g, TASK), "1-planted"));
    const got = await install(g, fakeArgus().source);
    expect(got.ok).toBe(true);
    expect(readFileSync(path.join(outside, "keep"), "utf8")).toBe("x");
  });
});

describe("recheckTaskIntegrations", () => {
  async function installed(g: string, argus: ReturnType<typeof fakeArgus>) {
    const got = await install(g, argus.source);
    if (!got.ok || !got.installed) throw new Error("not installed");
    return got.installed;
  }

  it("keeps the file while everything is ready and unchanged", async () => {
    const g = group();
    const argus = fakeArgus();
    const inst = await installed(g, argus);
    const r = await recheckTaskIntegrations(g, inst, argus.source);
    expect(r.state).toBe("kept");
    expect(existsSync(hostPath(g, inst.kubeconfig))).toBe(true);
  });

  it.each(["revoked", "expired", "store_unavailable"] as const)(
    "removes the file when the integration becomes %s mid-attempt",
    async (readiness) => {
      const g = group();
      const argus = fakeArgus();
      const inst = await installed(g, argus);
      argus.rows[0].readiness = readiness;
      const r = await recheckTaskIntegrations(g, inst, argus.source);
      expect(r).toMatchObject({ state: "removed", installed: null });
      expect(r.why).toContain(readiness);
      expect(existsSync(taskIntegrationsPath(g, TASK))).toBe(false);
    },
  );

  it("removes the file when the lease is gone, keeps it on a transient failure", async () => {
    const g = group();
    const argus = fakeArgus();
    const inst = await installed(g, argus);
    argus.failStatus(new ApiError(503, "unavailable"));
    expect((await recheckTaskIntegrations(g, inst, argus.source)).state).toBe("kept");
    argus.failStatus(new TypeError("fetch failed"));
    expect((await recheckTaskIntegrations(g, inst, argus.source)).state).toBe("kept");
    argus.failStatus(new ApiError(409, "stale_attempt"));
    const r = await recheckTaskIntegrations(g, inst, argus.source);
    expect(r).toMatchObject({ state: "removed", why: "stale_attempt" });
    expect(existsSync(inst.dir)).toBe(false);
  });

  it("replaces the file atomically in place after a rotation", async () => {
    const g = group();
    const argus = fakeArgus();
    const inst = await installed(g, argus);
    const file = hostPath(g, inst.kubeconfig);
    const before = lstatSync(file).ino;
    argus.rows[0].secret_version = 2;
    argus.rows[0].token = `${SECRET}-v2`;
    const route = fakeRoute();
    const r = await recheckTaskIntegrations(g, inst, argus.source, { transport: route.transport });
    expect(r.state).toBe("refreshed");
    // The rotated credential got a fresh, proven grant before it replaced the file.
    expect(route.opened.map((o) => o.who)).toEqual([{ taskId: TASK, attempt: 1 }]);
    expect(r.installed?.kubeconfig).toBe(inst.kubeconfig);
    expect(r.installed?.integrations[0].secret_version).toBe(2);
    expect(readFileSync(file, "utf8")).toContain(`${SECRET}-v2`);
    expect(lstatSync(file).ino).not.toBe(before);
    expect(statSync(file).mode & 0o777).toBe(0o600);
    expect(readdirSync(path.dirname(file))).toEqual([KUBECONFIG_FILE]);
  });

  it("re-fetches and re-grants after an admin edits the config", async () => {
    const g = group();
    const argus = fakeArgus();
    const inst = await installed(g, argus);
    argus.rows[0].config_version = 2;
    argus.rows[0].namespace = "team-b";
    const route = fakeRoute();
    const r = await recheckTaskIntegrations(g, inst, argus.source, { transport: route.transport });
    expect(r.state).toBe("refreshed");
    expect(route.opened.map((o) => o.who)).toEqual([{ taskId: TASK, attempt: 1 }]);
    expect(r.installed?.integrations[0]).toMatchObject({ config_version: 2, namespace: "team-b" });
    expect(readFileSync(hostPath(g, inst.kubeconfig), "utf8")).toContain("team-b");
  });

  it("removes the file and revokes when the rotated credential gets no route", async () => {
    const g = group();
    const argus = fakeArgus();
    const inst = await installed(g, argus);
    argus.rows[0].secret_version = 2;
    const route = fakeRoute(() => ({
      ok: false,
      reason: "env_integration_gateway_intercepts",
      error: "intercepted",
    }));
    const r = await recheckTaskIntegrations(g, inst, argus.source, { transport: route.transport });
    expect(r).toMatchObject({ state: "removed", installed: null });
    expect(existsSync(taskIntegrationsPath(g, TASK))).toBe(false);
    expect(route.revoked).toEqual([[TASK, undefined]]);
  });

  it.each([
    ["revoked", () => undefined],
    [
      "the lease lost",
      (a: ReturnType<typeof fakeArgus>) => a.failStatus(new ApiError(409, "stale_attempt")),
    ],
  ] as const)("revokes the route grants when the integration is %s", async (_what, change) => {
    const g = group();
    const argus = fakeArgus();
    const inst = await installed(g, argus);
    argus.rows[0].readiness = "revoked";
    change(argus);
    const route = fakeRoute();
    const r = await recheckTaskIntegrations(g, inst, argus.source, { transport: route.transport });
    expect(r.state).toBe("removed");
    expect(route.revoked).toEqual([[TASK, undefined]]);
    expect(route.opened).toEqual([]);
  });

  it("removes the file when the selection changes under the attempt", async () => {
    const g = group();
    const argus = fakeArgus();
    const inst = await installed(g, argus);
    argus.rows.push({ ...argus.rows[0], integration_id: "int-9", name: "late" });
    const r = await recheckTaskIntegrations(g, inst, argus.source);
    expect(r.state).toBe("removed");
  });
});

/** A broker that records what it is asked; it grants every server unless told otherwise. */
function fakeBroker(answer?: (routes: readonly McpRoute[]) => McpOpenResult) {
  const opened: Array<{ who: { taskId: string; attempt: number }; routes: McpRoute[] }> = [];
  const renewed: Array<[string, number]> = [];
  const revoked: Array<[string, number | undefined]> = [];
  const broker: McpTransport = {
    open: async (who, routes) => {
      opened.push({ who, routes: [...routes] });
      return answer
        ? answer(routes)
        : {
            ok: true,
            servers: Object.fromEntries(
              routes.map((r) => [
                r.integration_id,
                {
                  type: "http",
                  url: `http://host.docker.internal:7001/mcp/grant-${r.integration_id}`,
                },
              ]),
            ),
          };
    },
    renew: (taskId, attempt) => {
      renewed.push([taskId, attempt]);
    },
    revoke: (taskId, attempt) => {
      revoked.push([taskId, attempt]);
    },
  };
  const sets: Array<Record<string, McpServerEntry>> = [];
  const sessionMcp = {
    set: async (servers: Record<string, McpServerEntry>) => {
      sets.push(servers);
    },
  };
  return { opened, renewed, revoked, broker, sets, sessionMcp };
}

describe("MCP integrations", () => {
  const MCP = { kind: "mcp" as const, integration_id: "mcp-0", name: "radar" };

  it("replaces only the session's argus-integration servers", () => {
    const argus = { type: "http", url: "http://argus/mcp" };
    const now = { type: "http" as const, url: "http://broker/mcp/new" };
    expect(
      withIntegrationServers(
        { argus, "argus-integration-old": { type: "sse", url: "http://broker/mcp/old" } },
        { "argus-integration-radar": now },
      ),
    ).toEqual({ argus, "argus-integration-radar": now });
    expect(withIntegrationServers(["not", "an", "object"], {})).toEqual({});
  });

  it("grants the broker the token and gives the session only grant URLs", async () => {
    const g = group();
    const argus = fakeArgus([
      MCP,
      { kind: "mcp", integration_id: "mcp-1", name: "rw", tools: null },
    ]);
    const b = fakeBroker();
    const got = await install(g, argus.source, {
      selected: ["mcp-0", "mcp-1"],
      mcp: b.broker,
      sessionMcp: b.sessionMcp,
    });
    expect(got.ok).toBe(true);
    if (!got.ok || !got.installed) throw new Error("not installed");
    expect(b.opened[0]!.who).toEqual({ taskId: TASK, attempt: 1 });
    expect(b.opened[0]!.routes.map((r) => [r.name, r.tool_policy, r.tools])).toEqual([
      ["radar", "allowlist", ["search"]],
      ["rw", "all", null],
    ]);
    expect(b.opened[0]!.routes[0]!.token).toBe(`${SECRET}-0`);
    expect(b.sets).toEqual([
      {
        "argus-integration-radar": {
          type: "http",
          url: "http://host.docker.internal:7001/mcp/grant-mcp-0",
        },
        "argus-integration-rw": {
          type: "http",
          url: "http://host.docker.internal:7001/mcp/grant-mcp-1",
        },
      },
    ]);
    // No kubeconfig for MCP alone; the prompt names servers and tools, never a URL or token.
    expect(got.installed.kubeconfig).toBeNull();
    const lines = integrationLines(got.installed).join("\n");
    expect(lines).toContain("`mcp__argus-integration-radar__search`");
    expect(lines).toContain("every tool it lists");
    expect(lines).not.toMatch(/kubectl|grant-|host\.docker|fake-token/);
    expect(JSON.stringify(got.installed)).not.toContain(SECRET);
  });

  it("every grant asks Argus for exactly the attempt and versions it was given", async () => {
    const argus = fakeArgus([MCP]);
    const b = fakeBroker();
    await install(group(), argus.source, {
      selected: ["mcp-0"],
      attempt: 2,
      mcp: b.broker,
      sessionMcp: b.sessionMcp,
    });
    const authorize = b.opened[0]!.routes[0]!.authorize;
    argus.calls.splice(0);
    expect(await authorize()).toBe(true);
    expect(argus.calls).toEqual([`status:${TASK}:2`]);
    argus.rows[0]!.config_version = 2; // an admin edit
    expect(await authorize()).toBe(false);
    argus.rows[0]!.config_version = 1;
    argus.rows[0]!.readiness = "revoked";
    expect(await authorize()).toBe(false);
    argus.failStatus(new ApiError(409, "stale_attempt")); // the lease is gone
    await expect(authorize()).rejects.toThrow();
  });

  it.each([
    ["no broker", { mcp: undefined }],
    ["no session config", { sessionMcp: undefined }],
  ])("fails closed with %s", async (_what, over) => {
    const g = group();
    const b = fakeBroker();
    const got = await install(g, fakeArgus([MCP]).source, {
      selected: ["mcp-0"],
      mcp: b.broker,
      sessionMcp: b.sessionMcp,
      ...over,
    });
    expect(got).toMatchObject({ ok: false, reason: "env_integration_route_unavailable" });
  });

  it("a broker refusal blocks, revokes and leaves no server in the session config", async () => {
    const g = group();
    const b = fakeBroker(() => ({
      ok: false,
      reason: "env_integration_unavailable",
      error: "radar: the MCP server refused the credential (HTTP 401)",
    }));
    const got = await install(g, fakeArgus([MCP]).source, {
      selected: ["mcp-0"],
      mcp: b.broker,
      sessionMcp: b.sessionMcp,
    });
    expect(got).toMatchObject({ ok: false, reason: "env_integration_unavailable" });
    expect(b.revoked).toEqual([[TASK, undefined]]);
    expect(b.sets).toEqual([{}]);
  });

  it("refuses an engine that cannot be confined, before any credential is fetched", async () => {
    const argus = fakeArgus([MCP]);
    const b = fakeBroker();
    const got = await install(group(), argus.source, {
      selected: ["mcp-0"],
      engineProvider: "openai",
      mcp: b.broker,
      sessionMcp: b.sessionMcp,
    });
    expect(got).toMatchObject({ ok: false, reason: "env_integration_unsupported" });
    expect(argus.calls).toEqual([]);
    expect(b.opened).toEqual([]);
  });

  it("refuses an allowlist removed after selection (tools_unknown)", async () => {
    const b = fakeBroker();
    const got = await install(group(), fakeArgus([{ ...MCP, readiness: "tools_unknown" }]).source, {
      selected: ["mcp-0"],
      mcp: b.broker,
      sessionMcp: b.sessionMcp,
    });
    expect(got).toMatchObject({ ok: false, reason: "env_integration_unavailable" });
    expect(b.opened).toEqual([]);
  });

  it("a kept recheck renews the grants; an edit re-grants; a revocation clears the session", async () => {
    const g = group();
    const argus = fakeArgus([MCP]);
    const b = fakeBroker();
    const got = await install(g, argus.source, {
      selected: ["mcp-0"],
      mcp: b.broker,
      sessionMcp: b.sessionMcp,
    });
    if (!got.ok || !got.installed) throw new Error("not installed");
    const t = { mcp: b.broker, sessionMcp: b.sessionMcp };
    let r = await recheckTaskIntegrations(g, got.installed, argus.source, t);
    expect(r.state).toBe("kept");
    expect(b.renewed).toEqual([[TASK, 1]]);

    argus.rows[0]!.config_version = 2;
    r = await recheckTaskIntegrations(g, got.installed, argus.source, t);
    expect(r.state).toBe("refreshed");
    expect(b.opened).toHaveLength(2);
    expect(r.installed?.integrations[0]?.config_version).toBe(2);

    argus.rows[0]!.readiness = "revoked";
    r = await recheckTaskIntegrations(g, r.installed!, argus.source, t);
    expect(r).toMatchObject({ state: "removed", why: "radar (revoked)" });
    expect(b.revoked).toEqual([[TASK, undefined]]);
    expect(b.sets.at(-1)).toEqual({});
  });

  it("a lost lease removes the servers", async () => {
    const g = group();
    const argus = fakeArgus([MCP]);
    const b = fakeBroker();
    const got = await install(g, argus.source, {
      selected: ["mcp-0"],
      mcp: b.broker,
      sessionMcp: b.sessionMcp,
    });
    if (!got.ok || !got.installed) throw new Error("not installed");
    argus.failStatus(new ApiError(409, "stale_attempt"));
    const r = await recheckTaskIntegrations(g, got.installed, argus.source, {
      mcp: b.broker,
      sessionMcp: b.sessionMcp,
    });
    expect(r).toMatchObject({ state: "removed", why: "stale_attempt" });
    expect(b.revoked).toEqual([[TASK, undefined]]);
    expect(b.sets.at(-1)).toEqual({});
  });

  it("installs Kubernetes and MCP side by side", async () => {
    const g = group();
    const b = fakeBroker();
    const got = await install(g, fakeArgus([{}, MCP]).source, {
      selected: ["int-0", "mcp-0"],
      mcp: b.broker,
      sessionMcp: b.sessionMcp,
    });
    if (!got.ok || !got.installed?.kubeconfig) throw new Error("not installed");
    const cfg = JSON.parse(readFileSync(hostPath(g, got.installed.kubeconfig), "utf8"));
    expect(cfg.clusters.map((c: { name: string }) => c.name)).toEqual(["cluster-0"]);
    expect(Object.keys(b.sets[0]!)).toEqual(["argus-integration-radar"]);
    const lines = integrationLines(got.installed);
    expect(lines).toContain("Kubernetes access (this attempt only):");
    expect(lines.join("\n")).toContain("argus-integration-radar");
  });
});

describe("cleanup", () => {
  it("removeTaskIntegrations is idempotent and leaves other tasks alone", async () => {
    const g = group();
    const other = randomUUID();
    const argus = fakeArgus();
    await install(g, argus.source);
    await install(g, argus.source, { taskId: other });
    await removeTaskIntegrations(g, TASK);
    await removeTaskIntegrations(g, TASK);
    expect(existsSync(taskIntegrationsPath(g, TASK))).toBe(false);
    expect(existsSync(taskIntegrationsPath(g, other))).toBe(true);
    await removeTaskIntegrations(group(), TASK); // no root at all
  });

  it("sweepTaskIntegrations empties the root at startup", async () => {
    const g = group();
    await install(g, fakeArgus().source);
    await sweepTaskIntegrations(g);
    expect(readdirSync(path.join(g, TASK_INTEGRATIONS_DIR))).toEqual([]);
    await sweepTaskIntegrations(group());
  });
});

it("renderKubeconfig never emits insecure-skip-tls-verify, nor a cluster without its route", () => {
  const cred: KubernetesCredential = {
    kind: "kubernetes",
    integration_id: "i",
    name: "c",
    server: "https://x.test",
    namespace: "ns",
    ca_data: null,
    access: "read_only",
    token: "t",
    secret_version: 1,
    config_version: 1,
    expires_at: null,
  };
  const text = renderKubeconfig([cred], { i: grantUrl("i") });
  expect(text).not.toMatch(/insecure/i);
  expect(JSON.parse(text).contexts[0].context.namespace).toBe("ns");
  expect(() => renderKubeconfig([cred], {})).toThrow("a cluster has no route");
});

it("internalAddress refuses what the API's internal_address refuses", () => {
  for (const ip of [
    "127.0.0.1",
    "::1",
    "169.254.169.254",
    "0.0.0.0",
    "::",
    "fe80::1",
    "224.0.0.1",
    "240.0.0.1",
    "::ffff:127.0.0.1",
    "nonsense",
  ])
    expect(internalAddress(ip), ip).toBe(true);
  for (const ip of ["10.0.0.1", "172.17.0.2", "192.168.1.1", "203.0.113.7", "2001:db8::1"])
    expect(internalAddress(ip), ip).toBe(false);
});

/** A host address the route may dial (it refuses loopback), or null on a host without one. */
const HOST_IP =
  Object.values(os.networkInterfaces())
    .flat()
    .find((a) => a && a.family === "IPv4" && !a.internal)?.address ?? null;

/**
 * The route against real TLS. The cluster (an HTTPS server on loopback) is reached through
 * a recording TCP relay on a non-loopback address: the relay sees exactly the bytes the
 * route forwards. An impostor with another CA stands for anything that terminates TLS.
 */
describe.skipIf(!HOST_IP)("createKubernetesRoute (skipped: no non-loopback IPv4 to dial)", () => {
  const ip = HOST_IP!;
  const NAME = "k8s.example.test";
  let pki: { clusterCa: string; impostorCa: string };
  const servers: net.Server[] = [];
  let clusterPort = 0;
  let impostorPort = 0;
  /** Requests the cluster served, with their bearer. */
  const served: Array<{ url: string; authorization: string | undefined }> = [];
  let relay: { port: number; bytes: Buffer[]; accepted: number; to: "cluster" | "impostor" };
  let routes: KubernetesRoute[] = [];
  let addresses: Record<string, string[]>;
  let lookups: string[] = [];
  let allowed: () => Promise<boolean>;
  let authorized = 0;

  function openssl(dir: string, ...args: string[]) {
    execFileSync("openssl", args, { cwd: dir, stdio: "pipe" });
  }

  /** A CA, and a leaf it signs for k8s.example.test and the host address. */
  function authority(dir: string, name: string) {
    const ec = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes"];
    openssl(
      dir,
      "req",
      "-x509",
      ...ec,
      "-keyout",
      `${name}-ca.key`,
      "-out",
      `${name}-ca.pem`,
      "-days",
      "2",
      "-subj",
      `/CN=${name} test CA`,
    );
    writeFileSync(
      path.join(dir, `${name}.ext`),
      `subjectAltName=DNS:${NAME},IP:${ip}\nbasicConstraints=CA:FALSE\n`,
    );
    openssl(dir, "req", ...ec, "-keyout", `${name}.key`, "-out", `${name}.csr`, "-subj", "/CN=k8s");
    openssl(
      dir,
      "x509",
      "-req",
      "-in",
      `${name}.csr`,
      "-CA",
      `${name}-ca.pem`,
      "-CAkey",
      `${name}-ca.key`,
      "-CAcreateserial",
      "-out",
      `${name}.pem`,
      "-days",
      "2",
      "-extfile",
      `${name}.ext`,
    );
    const read = (f: string) => readFileSync(path.join(dir, f), "utf8");
    return { ca: read(`${name}-ca.pem`), key: read(`${name}.key`), cert: read(`${name}.pem`) };
  }

  function listen(server: net.Server, host: string) {
    servers.push(server);
    return new Promise<number>((resolve) =>
      server.listen(0, host, () => resolve((server.address() as net.AddressInfo).port)),
    );
  }

  beforeAll(async () => {
    const dir = mkdtempSync(path.join(os.tmpdir(), "argus-route-pki-"));
    try {
      const cluster = authority(dir, "cluster");
      const impostor = authority(dir, "impostor");
      pki = { clusterCa: cluster.ca, impostorCa: impostor.ca };
      const serve = (key: string, cert: string) =>
        https.createServer({ key, cert }, (req, res) => {
          served.push({ url: req.url ?? "", authorization: req.headers.authorization });
          res.end("ok");
        });
      clusterPort = await listen(serve(cluster.key, cluster.cert), "127.0.0.1");
      impostorPort = await listen(serve(impostor.key, impostor.cert), "127.0.0.1");
      relay = { port: 0, bytes: [], accepted: 0, to: "cluster" };
      const relayServer = net.createServer((sock) => {
        relay.accepted++;
        const up = net.connect(relay.to === "cluster" ? clusterPort : impostorPort, "127.0.0.1");
        sock.on("data", (c: Buffer) => relay.bytes.push(c));
        up.on("data", (c: Buffer) => relay.bytes.push(c));
        sock.pipe(up);
        up.pipe(sock);
        sock.on("error", () => up.destroy());
        up.on("error", () => sock.destroy());
        sock.on("close", () => up.destroy());
        up.on("close", () => sock.destroy());
      });
      relay.port = await listen(relayServer, ip);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  afterAll(async () => {
    for (const s of servers) {
      (s as { closeAllConnections?: () => void }).closeAllConnections?.();
      await new Promise((r) => s.close(r));
    }
  });

  afterEach(async () => {
    for (const r of routes.splice(0)) await r.close();
    relay.bytes.splice(0);
    relay.accepted = 0;
    relay.to = "cluster";
    served.splice(0);
    lookups = [];
    authorized = 0;
  });

  function newRoute(over: Partial<Parameters<typeof createKubernetesRoute>[0]> = {}) {
    addresses = {
      [NAME]: [ip],
      "wrong.example.test": [ip],
      "meta.example.test": ["169.254.169.254"],
    };
    allowed = async () => true;
    const r = createKubernetesRoute({
      bindHost: async () => "127.0.0.1",
      lookup: async (host) => {
        lookups.push(host);
        const got = addresses[host];
        if (!got) throw Object.assign(new Error("not found"), { code: "ENOTFOUND" });
        return got;
      },
      ...over,
    });
    routes.push(r);
    return r;
  }

  const b64 = (pem: string) => Buffer.from(pem).toString("base64");
  const route = (over: Partial<IntegrationRoute> = {}): IntegrationRoute => ({
    integration_id: "int-0",
    name: "prod",
    server: `https://${NAME}:${relay.port}`,
    ca_data: b64(pki.clusterCa),
    authorize: async () => {
      authorized++;
      return allowed();
    },
    ...over,
  });
  const A1 = { taskId: TASK, attempt: 1 };

  async function granted(r: KubernetesRoute, over: Partial<IntegrationRoute> = {}, who = A1) {
    const got = await r.open(who, [route(over)]);
    if (!got.ok) throw new Error(got.error);
    return got.proxies["int-0"]!;
  }

  /**
   * What kubectl does with a cluster's `proxy-url`: CONNECT with the userinfo as Basic
   * proxy authorization, then TLS verified against the cluster CA, then the request.
   */
  function viaRoute(
    proxyUrl: string,
    opts: { authority?: string; auth?: string | null; method?: string; keepOpen?: boolean } = {},
  ): Promise<{ connect: number; tls?: string; body?: string; socket?: tls.TLSSocket }> {
    const u = new URL(proxyUrl);
    const authority = opts.authority ?? `${NAME}:${relay.port}`;
    const auth =
      opts.auth === undefined
        ? Buffer.from(
            `${decodeURIComponent(u.username)}:${decodeURIComponent(u.password)}`,
          ).toString("base64")
        : opts.auth;
    return new Promise((resolve, reject) => {
      const sock = net.connect(Number(u.port), u.hostname);
      sock.on("error", reject);
      sock.on("connect", () =>
        sock.write(
          `${opts.method ?? "CONNECT"} ${authority} HTTP/1.1\r\nHost: ${authority}\r\n` +
            `${auth === null ? "" : `Proxy-Authorization: Basic ${auth}\r\n`}\r\n`,
        ),
      );
      let head = Buffer.alloc(0);
      const onData = (c: Buffer) => {
        head = Buffer.concat([head, c]);
        const end = head.indexOf("\r\n\r\n");
        if (end < 0) return;
        sock.off("data", onData);
        const connect = Number(head.subarray(9, 12).toString());
        if (connect !== 200) return resolve({ connect });
        const t = tls.connect({
          socket: sock,
          servername: authority.split(":")[0],
          ca: pki.clusterCa,
        });
        t.on("error", (err) => resolve({ connect, tls: (err as NodeJS.ErrnoException).code }));
        t.on("secureConnect", () => {
          if (opts.keepOpen) return resolve({ connect, socket: t });
          t.write(
            `GET /api/v1/namespaces/team-a/pods HTTP/1.1\r\nHost: ${authority}\r\nAuthorization: Bearer ${SECRET}\r\nConnection: close\r\n\r\n`,
          );
          const body: Buffer[] = [];
          t.on("data", (d: Buffer) => body.push(d));
          t.on("end", () => resolve({ connect, body: Buffer.concat(body).toString() }));
        });
      };
      sock.on("data", onData);
    });
  }

  const closed = (s: net.Socket) =>
    s.destroyed ? Promise.resolve() : new Promise<void>((r) => s.once("close", () => r()));

  it("proves the route, then carries kubectl's TLS end to end with the bearer encrypted", async () => {
    const r = newRoute();
    const proxy = await granted(r);
    expect(proxy).toMatch(/^http:\/\/argus:[A-Za-z0-9_-]{43}@127\.0\.0\.1:\d+$/);
    // The proof was a CONNECT through the listener (authorized) and a handshake only.
    expect(authorized).toBe(1);
    expect(served).toEqual([]);
    const got = await viaRoute(proxy);
    expect(got.connect).toBe(200);
    expect(got.body).toMatch(/^HTTP\/1\.1 200 OK[\s\S]*\r\n\r\nok$/);
    expect(served).toEqual([
      { url: "/api/v1/namespaces/team-a/pods", authorization: `Bearer ${SECRET}` },
    ]);
    expect(authorized).toBe(2);
    // The route forwarded ciphertext only: no request line, header or bearer.
    const wire = Buffer.concat(relay.bytes).toString("latin1");
    expect(wire.length).toBeGreaterThan(0);
    expect(wire).not.toMatch(/Bearer|Authorization|GET \/|HTTP\/1|team-a/);
    expect(wire).not.toContain(SECRET);
    expect(lookups).toEqual([NAME]);
  });

  it("answers 407 without a known grant, 405 to anything but CONNECT", async () => {
    const r = newRoute();
    const proxy = await granted(r);
    const forged = new URL(proxy);
    forged.password = "A".repeat(43);
    expect((await viaRoute(forged.toString())).connect).toBe(407);
    expect((await viaRoute(proxy, { auth: null })).connect).toBe(407);
    expect((await viaRoute(proxy, { auth: Buffer.from("argus").toString("base64") })).connect).toBe(
      407,
    );
    expect((await viaRoute(proxy, { method: "GET" })).connect).toBe(405);
    expect(relay.accepted).toBe(1); // the proof only
  });

  it("refuses (403) any destination but the granted host:port, whatever the session asks", async () => {
    const r = newRoute();
    const proxy = await granted(r);
    for (const authority of [
      `${NAME}:443`,
      `${NAME}:${relay.port + 1}`,
      `wrong.example.test:${relay.port}`,
      `${ip}:${relay.port}`,
      `127.0.0.1:${clusterPort}`,
      `169.254.169.254:80`,
    ])
      expect((await viaRoute(proxy, { authority })).connect, authority).toBe(403);
    expect(relay.accepted).toBe(1);
  });

  it("refuses (403) a tunnel Argus no longer authorizes, or cannot be asked about", async () => {
    const r = newRoute();
    const proxy = await granted(r);
    allowed = async () => false;
    expect((await viaRoute(proxy)).connect).toBe(403);
    allowed = async () => {
      throw new Error("Argus unreachable");
    };
    expect((await viaRoute(proxy)).connect).toBe(403);
    expect(relay.accepted).toBe(1);
  });

  it("dials only the addresses resolved at grant time", async () => {
    const r = newRoute();
    const proxy = await granted(r);
    // The name now resolves elsewhere (a rebinding): the grant keeps its pinned address.
    addresses[NAME] = ["127.0.0.1"];
    expect((await viaRoute(proxy)).body).toMatch(/ok$/);
    expect(relay.accepted).toBe(2);
    expect(lookups).toEqual([NAME]);
    // A new grant resolves again, and refuses loopback.
    expect(await r.open(A1, [route()])).toEqual({
      ok: false,
      reason: "env_integration_route_unavailable",
      error: "prod: the server resolves to a loopback or link-local address",
    });
  });

  it.each([
    [`https://meta.example.test:443`, "resolves to a loopback or link-local address"],
    [`https://127.0.0.1:443`, "resolves to a loopback or link-local address"],
    [`https://[::1]:443`, "resolves to a loopback or link-local address"],
    [`https://nowhere.example.test`, "does not resolve (ENOTFOUND)"],
  ])("refuses a server at %s before any grant exists", async (server, why) => {
    const r = newRoute();
    const got = await r.open(A1, [route({ server })]);
    expect(got).toEqual({
      ok: false,
      reason: "env_integration_route_unavailable",
      error: `prod: the server ${why}`,
    });
    expect(relay.accepted).toBe(0);
  });

  it("revocation closes open tunnels at once and refuses new ones", async () => {
    const r = newRoute();
    const proxy = await granted(r);
    const open = await viaRoute(proxy, { keepOpen: true });
    expect(open.connect).toBe(200);
    r.revoke(TASK, 2); // another attempt's: nothing of this one
    expect(open.socket!.destroyed).toBe(false);
    r.revoke(TASK);
    await closed(open.socket!);
    expect((await viaRoute(proxy)).connect).toBe(407);
  });

  it("a new attempt's grant fences the previous attempt's, tunnels included", async () => {
    const r = newRoute();
    const first = await granted(r);
    const open = await viaRoute(first, { keepOpen: true });
    const second = await granted(r, {}, { taskId: TASK, attempt: 2 });
    await closed(open.socket!);
    expect(second).not.toBe(first);
    expect((await viaRoute(first)).connect).toBe(407);
    expect((await viaRoute(second)).body).toMatch(/ok$/);
    // Another task's grant is its own: revoking this task leaves it.
    const other = await granted(r, {}, { taskId: randomUUID(), attempt: 1 });
    r.revoke(TASK);
    expect((await viaRoute(other)).body).toMatch(/ok$/);
  });

  it("closes a tunnel after its maximum lifetime", async () => {
    const r = newRoute({ tunnelMaxMs: 50 });
    const open = await viaRoute(await granted(r), { keepOpen: true });
    expect(open.connect).toBe(200);
    await closed(open.socket!);
  });

  it.each([
    [
      "a CA that is not the cluster's",
      () => ({ ca_data: b64(pki.impostorCa) }),
      "cluster",
      /not trusted by the integration's CA \([A-Z_]+\); something intercepts TLS/,
    ],
    [
      "no CA: the public roots, never the process's extras",
      () => ({ ca_data: null }),
      "cluster",
      /not trusted/,
    ],
    ["a certificate from something that terminates TLS", () => ({}), "impostor", /not trusted/],
    [
      "a certificate that does not name the server",
      () => ({ server: `https://wrong.example.test:${relay.port}` }),
      "cluster",
      /does not name the server \(ERR_TLS_CERT_ALTNAME_INVALID\)/,
    ],
  ] as const)("refuses %s, and leaves no grant", async (_what, over, to, why) => {
    const r = newRoute();
    relay.to = to;
    const got = await r.open(A1, [route(over())]);
    expect(got).toMatchObject({ ok: false, reason: "env_integration_gateway_intercepts" });
    if (got.ok) throw new Error("granted");
    expect(got.error).toMatch(why);
    expect(got.error).toMatch(/^prod: /);
    expect(served).toEqual([]);
  });

  it("is unavailable without a bridge, on a bad port, or when the cluster is down", async () => {
    expect(await newRoute({ bindHost: async () => null }).open(A1, [route()])).toEqual({
      ok: false,
      reason: "env_integration_route_unavailable",
      error: "this worker has no docker bridge address for the Kubernetes route",
    });
    expect(await newRoute({ port: 70000 }).open(A1, [route()])).toMatchObject({
      ok: false,
      reason: "env_integration_route_unavailable",
    });
    const down = net.createServer();
    const downPort = await new Promise<number>((res) =>
      down.listen(0, ip, () => res((down.address() as net.AddressInfo).port)),
    );
    await new Promise((res) => down.close(res));
    expect(await newRoute().open(A1, [route({ server: `https://${NAME}:${downPort}` })])).toEqual({
      ok: false,
      reason: "env_integration_route_unavailable",
      error: "prod: the route refused the CONNECT (HTTP 502)",
    });
  });

  it("the production factory accepts only a port number from the environment", async () => {
    const r = nanoclawKubernetesRoute({ ARGUS_INTEGRATION_ROUTE_PORT: "1-2" });
    routes.push(r);
    const got = await r.open(A1, [route()]);
    expect(got).toMatchObject({ ok: false, reason: "env_integration_route_unavailable" });
    if (got.ok) throw new Error("granted");
    // No bridge here, or the port is refused: either way, never a grant.
    expect(got.error).toMatch(/no docker bridge address|not a port number/);
  });
});
