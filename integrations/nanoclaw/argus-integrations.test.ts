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
import net from "node:net";
import os from "node:os";
import path from "node:path";
import tls from "node:tls";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import {
  type IntegrationRoute,
  type IntegrationState,
  type IntegrationsSource,
  installTaskIntegrations,
  integrationLines,
  KUBECONFIG_FILE,
  type KubernetesCredential,
  type RouteProbe,
  recheckTaskIntegrations,
  removeTaskIntegrations,
  renderKubeconfig,
  sweepTaskIntegrations,
  TASK_INTEGRATIONS_DIR,
  taskIntegrationsPath,
} from "./argus-integrations.js";
import { type GatewayProxy, gatewayRouteProbe } from "./argus-integrations-route.js";

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
  entries: Array<Partial<IntegrationState> & { token?: string; ca_data?: string | null }> = [{}],
) {
  const rows = entries.map((e, i) => ({
    integration_id: e.integration_id ?? `int-${i}`,
    name: e.name ?? `cluster-${i}`,
    namespace: e.namespace ?? "team-a",
    secret_version: e.secret_version ?? 1,
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
      return rows.map(({ token: _t, ca_data: _c, ...s }) => ({ ...s }));
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
      return {
        integration_id: r.integration_id,
        name: r.name,
        server: "https://k8s.example.test:6443",
        namespace: r.namespace,
        ca_data: r.ca_data,
        access: "read_only",
        token: r.token,
        secret_version: r.secret_version,
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
/** The route probe of a gateway that splices end to end: these tests are about the files. */
const endToEnd: RouteProbe = async () => null;

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
    routeProbe: endToEnd,
    ...over,
  });
}

function hostPath(groupDir: string, containerPath: string): string {
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
    expect(cfg.clusters[1].cluster).toEqual({ server: "https://k8s.example.test:6443" });
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

  it("blocks with the probe's reason and leaves no kubeconfig when the gateway intercepts", async () => {
    const g = group();
    const argus = fakeArgus();
    const stale = await install(g, argus.source);
    if (!stale.ok || !stale.installed) throw new Error("not installed");
    const seen: Array<{ routes: readonly IntegrationRoute[]; who: unknown }> = [];
    const routeProbe: RouteProbe = async (routes, who) => {
      seen.push({ routes, who });
      return "cluster-0: the gateway intercepts TLS";
    };
    const got = await install(g, argus.source, { attempt: 2, routeProbe });
    expect(got).toEqual({
      ok: false,
      reason: "env_integration_gateway_intercepts",
      error: "cluster-0: the gateway intercepts TLS",
    });
    expect(existsSync(taskIntegrationsPath(g, TASK))).toBe(false);
    // The probe sees where to go and whom it serves, never the token.
    expect(seen).toEqual([
      {
        routes: [
          {
            name: "cluster-0",
            server: "https://k8s.example.test:6443",
            ca_data: Buffer.from("PEM").toString("base64"),
          },
        ],
        who: { taskId: TASK, attempt: 2 },
      },
    ]);
    expect(JSON.stringify(seen)).not.toContain(SECRET);
  });

  it("fails closed without a route probe", async () => {
    const g = group();
    const got = await install(g, fakeArgus().source, { routeProbe: undefined });
    expect(got).toMatchObject({ ok: false, reason: "env_integration_gateway_intercepts" });
    expect(existsSync(taskIntegrationsPath(g, TASK))).toBe(false);
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
    const probe = vi.fn(endToEnd);
    const r = await recheckTaskIntegrations(g, inst, argus.source, probe);
    expect(r.state).toBe("refreshed");
    // The rotated credential was probed before it replaced the file.
    expect(probe).toHaveBeenCalledOnce();
    expect(r.installed?.kubeconfig).toBe(inst.kubeconfig);
    expect(r.installed?.integrations[0].secret_version).toBe(2);
    expect(readFileSync(file, "utf8")).toContain(`${SECRET}-v2`);
    expect(lstatSync(file).ino).not.toBe(before);
    expect(statSync(file).mode & 0o777).toBe(0o600);
    expect(readdirSync(path.dirname(file))).toEqual([KUBECONFIG_FILE]);
  });

  it("removes the file when the rotated credential no longer passes the route probe", async () => {
    const g = group();
    const argus = fakeArgus();
    const inst = await installed(g, argus);
    argus.rows[0].secret_version = 2;
    const r = await recheckTaskIntegrations(g, inst, argus.source, async () => "intercepted");
    expect(r).toMatchObject({ state: "removed", installed: null });
    expect(existsSync(taskIntegrationsPath(g, TASK))).toBe(false);
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

it("renderKubeconfig never emits insecure-skip-tls-verify", () => {
  const text = renderKubeconfig([
    {
      integration_id: "i",
      name: "c",
      server: "https://x.test",
      namespace: "ns",
      ca_data: null,
      access: "read_only",
      token: "t",
      secret_version: 1,
      expires_at: null,
    },
  ]);
  expect(text).not.toMatch(/insecure/i);
  expect(JSON.parse(text).contexts[0].context.namespace).toBe("ns");
});

/**
 * The route probe against real TLS: a local CONNECT proxy that either splices to the
 * cluster (the upstream prerequisite: an authenticated CONNECT passed through) or
 * terminates TLS with its own CA (what OneCLI v1.42.0 does for every authenticated CONNECT).
 */
describe("gatewayRouteProbe", () => {
  const AGENTS = { "task-a": "agent-token-a", "task-b": "agent-token-b" } as const;
  type Mode = "splice" | "intercept";
  let pki: { clusterCa: string; gatewayCa: string };
  let upstream: tls.Server;
  let proxy: net.Server;
  let proxyPort: number;
  let mode: Mode = "splice";
  /** What the proxy saw: the CONNECT authority and authorization, and every byte after. */
  const seen: Array<{ authority: string; authorization: string | null; bytes: Buffer[] }> = [];

  function openssl(dir: string, ...args: string[]) {
    execFileSync("openssl", args, { cwd: dir, stdio: "pipe" });
  }

  /** A CA, and a leaf it signs for k8s.example.test and 127.0.0.1. */
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
      "subjectAltName=DNS:k8s.example.test,IP:127.0.0.1\nbasicConstraints=CA:FALSE\n",
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

  const listening = (server: net.Server) =>
    new Promise<number>((resolve) =>
      server.listen(0, "127.0.0.1", () => resolve((server.address() as net.AddressInfo).port)),
    );

  beforeAll(async () => {
    const dir = mkdtempSync(path.join(os.tmpdir(), "argus-route-pki-"));
    try {
      const cluster = authority(dir, "cluster");
      const gateway = authority(dir, "gateway");
      pki = { clusterCa: cluster.ca, gatewayCa: gateway.ca };
      upstream = tls.createServer({ key: cluster.key, cert: cluster.cert }, (s) => s.end());
      const upstreamPort = await listening(upstream);
      const known = new Set(["k8s.example.test", "wrong.example.test", "127.0.0.1"]);
      proxy = net.createServer((sock) => {
        let head = Buffer.alloc(0);
        const onData = (chunk: Buffer) => {
          head = Buffer.concat([head, chunk]);
          const end = head.indexOf("\r\n\r\n");
          if (end < 0) return;
          sock.off("data", onData);
          const lines = head.subarray(0, end).toString("latin1").split("\r\n");
          const authority = lines[0].split(" ")[1];
          const authorization =
            lines.find((l) => /^proxy-authorization:/i.test(l))?.replace(/^[^:]+:\s*/, "") ?? null;
          const entry = { authority, authorization, bytes: [head.subarray(end + 4)] as Buffer[] };
          seen.push(entry);
          const token = authorization
            ? Buffer.from(authorization.replace(/^Basic /, ""), "base64")
                .toString()
                .split(":")[1]
            : null;
          if (!Object.values(AGENTS).includes(token as never))
            return void sock.end("HTTP/1.1 407 Proxy Authentication Required\r\n\r\n");
          if (!known.has(authority.replace(/:\d+$/, "")))
            return void sock.end("HTTP/1.1 403 Forbidden\r\n\r\n");
          sock.on("data", (c: Buffer) => entry.bytes.push(c));
          sock.write("HTTP/1.1 200 Connection Established\r\n\r\n");
          if (mode === "intercept") {
            const t = new tls.TLSSocket(sock, {
              isServer: true,
              key: gateway.key,
              cert: gateway.cert,
            });
            t.on("error", () => sock.destroy());
            return;
          }
          const up = net.connect(upstreamPort, "127.0.0.1");
          sock.on("data", (c: Buffer) => up.write(c));
          up.pipe(sock);
          up.on("error", () => sock.destroy());
          sock.on("error", () => up.destroy());
          sock.on("close", () => up.destroy());
        };
        sock.on("data", onData);
        sock.on("error", () => undefined);
      });
      proxyPort = await listening(proxy);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  afterAll(async () => {
    await new Promise((r) => proxy?.close(r));
    await new Promise((r) => upstream?.close(r));
  });

  afterEach(() => {
    mode = "splice";
    seen.splice(0);
  });

  const route = (over: Partial<IntegrationRoute> = {}): IntegrationRoute => ({
    name: "prod",
    server: "https://k8s.example.test:6443",
    ca_data: Buffer.from(pki.clusterCa).toString("base64"),
    ...over,
  });

  /** A lease per task: each task's session carries its own agent token. */
  function leases(proxyUrl?: (token: string) => string | null) {
    const released: string[] = [];
    const gatewayProxy = vi.fn(async (who: { taskId: string; attempt: number }) => {
      const token = AGENTS[who.taskId as keyof typeof AGENTS] ?? "agent-token-unknown";
      return {
        proxy: proxyUrl ? proxyUrl(token) : `http://x:${token}@127.0.0.1:${proxyPort}`,
        release: async () => {
          released.push(who.taskId);
        },
      } satisfies GatewayProxy;
    });
    return { released, gatewayProxy, probe: gatewayRouteProbe(gatewayProxy) };
  }
  const A = { taskId: "task-a", attempt: 1 };

  it("passes when the gateway splices the cluster's TLS end to end", async () => {
    const l = leases();
    expect(await l.probe([route(), route({ name: "ip", server: "https://127.0.0.1" })], A)).toBe(
      null,
    );
    expect(l.released).toEqual(["task-a"]);
    expect(seen.map((s) => s.authority)).toEqual(["k8s.example.test:6443", "127.0.0.1:443"]);
    // A handshake only: no HTTP request and no bearer token crossed the proxy in plaintext.
    const bytes = Buffer.concat(seen.flatMap((s) => s.bytes)).toString("latin1");
    expect(bytes).not.toMatch(/Bearer|Authorization|GET |HTTP\/1/);
    expect(bytes).not.toContain(SECRET);
  });

  it("refuses a certificate minted by an intercepting gateway", async () => {
    mode = "intercept";
    const l = leases();
    const why = await l.probe([route()], A);
    expect(why).toMatch(
      /^prod: .*not trusted by the integration's CA \([A-Z_]+\); the gateway intercepts TLS$/,
    );
    expect(l.released).toEqual(["task-a"]);
  });

  it("refuses a cluster CA mismatch, and never trusts the gateway's CA by default", async () => {
    const l = leases();
    const gatewayCa = Buffer.from(pki.gatewayCa).toString("base64");
    expect(await l.probe([route({ ca_data: gatewayCa })], A)).toMatch(/not trusted/);
    // No CA: the public roots only, even though the process may trust more.
    expect(await l.probe([route({ ca_data: null })], A)).toMatch(/not trusted/);
  });

  it("refuses a certificate that does not name the server", async () => {
    const l = leases();
    expect(await l.probe([route({ server: "https://wrong.example.test:6443" })], A)).toMatch(
      /does not name the server \(ERR_TLS_CERT_ALTNAME_INVALID\)/,
    );
  });

  it("fails on a refused CONNECT: unknown identity (407) or host (403)", async () => {
    const l = leases();
    expect(await l.probe([route()], { taskId: "task-z", attempt: 1 })).toBe(
      "prod: the gateway refused the CONNECT (HTTP 407)",
    );
    expect(await l.probe([route({ server: "https://elsewhere.test" })], A)).toBe(
      "prod: the gateway refused the CONNECT (HTTP 403)",
    );
    expect(l.released).toEqual(["task-z", "task-a"]);
  });

  it("carries each task's own identity and never names it in a reason", async () => {
    const l = leases();
    expect(await l.probe([route()], A)).toBe(null);
    expect(await l.probe([route()], { taskId: "task-b", attempt: 3 })).toBe(null);
    const tokens = seen.map((s) =>
      Buffer.from(s.authorization!.replace(/^Basic /, ""), "base64").toString(),
    );
    expect(tokens).toEqual(["x:agent-token-a", "x:agent-token-b"]);
    expect(l.gatewayProxy.mock.calls.map((c) => c[0])).toEqual([
      A,
      { taskId: "task-b", attempt: 3 },
    ]);
    mode = "intercept";
    expect(await l.probe([route()], A)).not.toContain("agent-token");
  });

  it("fails closed with no proxy, an unusable proxy, an unreachable one or no lease", async () => {
    const closed = net.createServer();
    const deadPort = await listening(closed);
    await new Promise((r) => closed.close(r));
    for (const [url, why] of [
      [null, "no gateway proxy is configured for this worker's sessions"],
      ["socks5://x:t@127.0.0.1:1080", "the session's gateway proxy is not a plain http URL"],
      [`http://x:t@127.0.0.1:${deadPort}`, "prod: the gateway proxy is unreachable (ECONNREFUSED)"],
    ] as const) {
      const l = leases(() => url);
      const got = await l.probe([route()], A);
      expect(got).toBe(why);
      expect(got).not.toContain(":t@");
      expect(l.released).toEqual(["task-a"]);
    }
    const failing = gatewayRouteProbe(async () => {
      throw Object.assign(new Error(`lease failed for ${SECRET}`), { code: "ECONNRESET" });
    });
    expect(await failing([route()], A)).toBe("the gateway lease for the probe failed (ECONNRESET)");
  });
});
