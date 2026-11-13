/**
 * Kubernetes and MCP integrations through the channel (integrations spec 3.6): a fake
 * Argus and OneCLI in one fetch, no network, no model, no cluster. The kubeconfig is
 * installed before the prompt, named but never quoted in it, removed (and its route
 * grants revoked) on revocation and when the attempt ends, and a non-deliverable
 * integration or an unproven route blocks the attempt. An MCP integration reaches the
 * session as a broker grant URL in its MCP config, removed the same ways.
 */
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChannelSetup } from "./adapter.js";
import { createArgusAdapter } from "./argus.js";
import { type AccountLease, OneCliClient } from "./argus-accounts.js";
import {
  type IntegrationRoute,
  type IntegrationTransport,
  KUBECONFIG_FILE,
  type McpServerEntry,
  type McpTransport,
  type RouteResult,
  TASK_INTEGRATIONS_DIR,
} from "./argus-integrations.js";
import { until } from "./argus-test-until.js";

vi.mock("../db/messaging-groups.js", () => ({
  getMessagingGroupByPlatform: async () => ({ id: "mg-1" }),
  getMessagingGroupAgents: async () => [{ agent_group_id: "group-1" }],
}));
vi.mock("../db/agent-groups.js", () => ({
  getAgentGroup: async (id: string) => ({ id, name: "argus worker", folder: "argus-worker" }),
}));
vi.mock("../db/sessions.js", () => ({
  getSessionsByAgentGroup: async () => [],
  findSessionForAgent: async () => undefined,
  updateSession: async () => undefined,
}));
vi.mock("./argus-local-forward.js", () => ({ syncLocalStrataForward: async () => undefined }));

const TASK = "7c0d2f4e-1a2b-4c3d-8e9f-0a1b2c3d4e5f";
const TOKEN = "fake-k8s-token-must-not-leak";
const tmp: string[] = [];
const live: Array<{ teardown: () => Promise<void> }> = [];
afterEach(async () => {
  for (const a of live.splice(0)) await a.teardown();
  for (const d of tmp.splice(0)) rmSync(d, { recursive: true, force: true });
});

const lease = (provider: string): AccountLease => ({
  lease_id: "l-1",
  account_id: "a-1",
  name: "engine",
  provider,
  kind: "api_key",
  credential:
    provider === "openai"
      ? { auth_mode: "apikey", OPENAI_API_KEY: "dummy-key" }
      : { api_key: "dummy-key" },
});

type Call = { method: string; path: string; body: any };

function world(provider: string, readiness: string, kind: "kubernetes" | "mcp" = "kubernetes") {
  const calls: Call[] = [];
  const state = { readiness, version: 1, rejectHeartbeat: false, holdRotation: false };
  let releaseOnHeartbeat: (() => void) | null = null;
  let task: object | undefined = {
    id: TASK,
    title: "Check the pods",
    spec: "List the pods.",
    project: "global",
    attempt: 1,
    acceptance: [],
    knowledge_refs: [],
    integrations: ["int-1"],
  };
  const leases = [lease(provider)];
  const agents: any[] = [];
  const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });
  const fetchImpl = (async (url: string, init: RequestInit) => {
    const u = new URL(url);
    const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
    const method = init.method ?? "GET";
    const p = u.pathname.replace(/^\/api/, "");
    if (u.hostname === "127.0.0.1") {
      if (method === "GET" && (p === "/agents" || p === "/secrets"))
        return json(200, p === "/agents" ? agents : []);
      if (method === "POST" && p === "/agents") {
        const a = { id: `ag-${agents.length}`, secretMode: "all", secrets: [], ...body };
        agents.push(a);
        return json(200, a);
      }
      if (method === "POST" && p === "/secrets") return json(200, { id: "sec", ...body });
      if (/\/secrets$/.test(p) && method === "GET") return json(200, []);
      return json(200, { success: true });
    }
    calls.push({ method, path: p, body });
    if (p === "/agents/register")
      return json(201, { token: "t", agent: { id: "ag" }, heartbeat_interval_s: 1 });
    if (p === "/agents/me/heartbeat") {
      releaseOnHeartbeat?.();
      return json(200, {
        state: "active",
        renewed: [],
        rejected: state.rejectHeartbeat ? [{ task_id: TASK, attempt: 1, code: "not_claimed" }] : [],
      });
    }
    if (p === "/accounts/lease") {
      const l = (body?.role ?? "engine") === "engine" ? leases.shift() : undefined;
      return l ? json(200, l) : json(409, { detail: { code: "no_account", message: "none" } });
    }
    if (p === "/tasks/claim") {
      const t = task;
      task = undefined;
      if (t) return json(200, t);
      return new Promise<Response>((_, reject) =>
        init.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        ),
      );
    }
    if (p === `/tasks/${TASK}/integrations/status`)
      return json(200, [
        {
          integration_id: "int-1",
          kind,
          name: "staging",
          namespace: kind === "kubernetes" ? "team-a" : null,
          tool_policy: kind === "mcp" ? "allowlist" : null,
          secret_version: state.version,
          config_version: 1,
          expires_at: "2026-12-01T00:00:00Z",
          readiness: state.readiness,
        },
      ]);
    if (p === `/tasks/${TASK}/integrations/int-1/credential`) {
      // A rotated fetch outlasts a heartbeat interval: the next one overlaps it.
      if (state.holdRotation && body.secret_version === 2)
        await new Promise<void>((r) => {
          releaseOnHeartbeat = r;
        });
      if (kind === "mcp")
        return json(200, {
          kind,
          integration_id: "int-1",
          name: "staging",
          url: "https://mcp.example.test/mcp",
          tools: ["search"],
          tool_policy: "allowlist",
          access: "read_only",
          token: `${TOKEN}-v${state.version}`,
          secret_version: state.version,
          config_version: 1,
          expires_at: "2026-12-01T00:00:00Z",
        });
      return json(200, {
        kind,
        integration_id: "int-1",
        name: "staging",
        server: "https://k8s.example.test:6443",
        namespace: "team-a",
        ca_data: null,
        access: "read_only",
        token: `${TOKEN}-v${state.version}`,
        secret_version: state.version,
        config_version: 1,
        expires_at: "2026-12-01T00:00:00Z",
      });
    }
    return json(200, {});
  }) as unknown as typeof fetch;
  return { calls, fetchImpl, state };
}

const PROXY = "http://argus:grant@172.17.0.1:7000";

/** A route that grants and records; `refuse` answers instead when it returns a result. */
function recordingRoute(refuse: () => RouteResult | null = () => null) {
  const opened: Array<[{ taskId: string; attempt: number }, IntegrationRoute[]]> = [];
  const revoked: Array<[string, number | undefined]> = [];
  const transport: IntegrationTransport = {
    open: async (who, routes) => {
      opened.push([who, [...routes]]);
      return (
        refuse() ?? {
          ok: true,
          proxies: Object.fromEntries(routes.map((x) => [x.integration_id, PROXY])),
        }
      );
    },
    revoke: (taskId, attempt) => {
      revoked.push([taskId, attempt]);
    },
  };
  return { opened, revoked, transport };
}

const GRANT = "http://172.17.0.1:7001/mcp/fake-grant";

/** An MCP broker and session config that grant and record. */
function recordingMcp() {
  const tokens: string[] = [];
  const revoked: Array<[string, number | undefined]> = [];
  let renewed = 0;
  const sessions: Array<Record<string, McpServerEntry>> = [];
  const broker: McpTransport = {
    open: async (_who, routes) => {
      tokens.push(...routes.map((x) => x.token));
      return {
        ok: true,
        servers: Object.fromEntries(
          routes.map((x) => [x.integration_id, { type: "http", url: GRANT }]),
        ),
      };
    },
    renew: () => {
      renewed++;
    },
    revoke: (taskId, attempt) => {
      revoked.push([taskId, attempt]);
    },
  };
  const sessionMcp = {
    set: async (servers: Record<string, McpServerEntry>) => {
      sessions.push(servers);
    },
  };
  return { tokens, revoked, sessions, broker, sessionMcp, renewed: () => renewed };
}

async function run(
  provider = "anthropic",
  readiness = "ready",
  route: ReturnType<typeof recordingRoute> | null = recordingRoute(),
  mcp?: ReturnType<typeof recordingMcp>,
  kind: "kubernetes" | "mcp" = mcp ? "mcp" : "kubernetes",
) {
  const w = world(provider, readiness, kind);
  const groupDir = mkdtempSync(path.join(os.tmpdir(), "argus-integrations-channel-"));
  tmp.push(groupDir);
  const inbound: any[] = [];
  let thread: string | null = null;
  const logs: string[] = [];
  const spy = vi.spyOn(console, "log").mockImplementation((...a) => {
    logs.push(a.map(String).join(" "));
  });
  const setup = {
    onInbound: vi.fn(async (_p: string, t: string | null, m: any) => {
      thread = t;
      inbound.push(m);
    }),
    onInboundEvent: vi.fn(),
    onMetadata: vi.fn(),
    onAction: vi.fn(),
  } as unknown as ChannelSetup;
  const recycleRuntime = vi.fn(async () => undefined);
  const adapter = createArgusAdapter({
    url: "http://argus.test",
    bootstrapToken: "boot",
    capabilities: [],
    name: "w",
    fetch: w.fetchImpl,
    claimWaitS: 1,
    heartbeatMs: 50,
    workspaceRoot: async () => groupDir,
    ...(route ? { integrationRoute: route.transport } : {}),
    ...(mcp ? { integrationMcp: mcp.broker, sessionMcp: mcp.sessionMcp } : {}),
    sessionImage: async () => "argus-nanoclaw-agent:test",
    readImageFile: async () =>
      readFileSync(new URL("./engines/claude-managed-settings.json", import.meta.url), "utf8"),
    accounts: {
      models: true,
      onecli: new OneCliClient("http://127.0.0.1:10254", w.fetchImpl),
      resolveTarget: async () => ({ identifier: "ag-group-1", name: "Worker" }),
      recycleRuntime,
    },
  });
  live.push({
    teardown: async () => {
      await adapter.teardown();
      spy.mockRestore();
    },
  });
  await adapter.setup(setup);
  const root = path.join(groupDir, TASK_INTEGRATIONS_DIR, TASK);
  const say = (text: string) =>
    adapter.deliver("local", thread, { kind: "chat", content: { text } } as never);
  const posts = (suffix: string) => w.calls.filter((c) => c.path === `/tasks/${TASK}${suffix}`);
  return { w, inbound, root, say, posts, logs, recycleRuntime, route };
}

const kubeconfigs = (root: string) =>
  existsSync(root)
    ? readdirSync(root).flatMap((d) =>
        readdirSync(path.join(root, d)).map((f) => path.join(root, d, f)),
      )
    : [];

describe("task integrations through the channel", () => {
  it("installs before the prompt, never quotes the token, and removes it on completion", async () => {
    const r = await run();
    await until(() => r.inbound.length > 0, 8000, "the task prompt");
    const [file] = kubeconfigs(r.root);
    expect(file).toMatch(/\/1-[0-9a-f-]{36}\/kubeconfig$/);
    expect(readFileSync(file!, "utf8")).toContain(TOKEN);
    expect(JSON.parse(readFileSync(file!, "utf8")).clusters[0].cluster["proxy-url"]).toBe(PROXY);
    const prompt = r.inbound[0].content.text as string;
    expect(prompt).toContain(
      `/workspace/agent/${TASK_INTEGRATIONS_DIR}/${TASK}/${path.basename(path.dirname(file!))}/kubeconfig`,
    );
    expect(prompt).toContain("namespace team-a");
    expect(prompt).not.toContain(TOKEN);
    // The credential was asked for this attempt at the version the status named.
    expect(r.posts("/integrations/int-1/credential")[0]!.body).toEqual({
      attempt: 1,
      secret_version: 1,
    });

    await r.say("COMPLETE: listed the pods");
    await until(() => r.posts("/complete").length > 0, 8000, "complete");
    await until(() => !existsSync(r.root), 8000, "the kubeconfig is removed");
    // The attempt's grants end with it: no new tunnel, open ones closed.
    expect(r.route!.revoked).toContainEqual([TASK, undefined]);
    expect(r.logs.join("\n")).not.toContain(TOKEN);
    expect(r.logs.join("\n")).not.toContain(PROXY);
  });

  it("removes the kubeconfig on the next heartbeat after a revocation", async () => {
    const r = await run();
    await until(() => r.inbound.length > 0, 8000, "the task prompt");
    expect(kubeconfigs(r.root)).toHaveLength(1);
    expect(r.route!.revoked).toEqual([]);
    r.w.state.readiness = "revoked";
    await until(() => kubeconfigs(r.root).length === 0, 8000, "the kubeconfig is removed");
    expect(r.route!.revoked).toContainEqual([TASK, undefined]);
    expect(r.posts("/complete")).toHaveLength(0);
  });

  it("replaces the kubeconfig on the next heartbeat after a rotation", async () => {
    const r = await run();
    await until(() => r.inbound.length > 0, 8000, "the task prompt");
    r.w.state.holdRotation = true;
    r.w.state.version = 2;
    // The rename is the write's last step: the new token visible means it finished.
    await until(
      () =>
        kubeconfigs(r.root).some(
          (f) =>
            path.basename(f) === KUBECONFIG_FILE && readFileSync(f, "utf8").includes(`${TOKEN}-v2`),
        ),
      8000,
      "the kubeconfig is replaced",
    );
    // One fetch and one write: the overlapping heartbeat did not start a second.
    const rotated = r
      .posts("/integrations/int-1/credential")
      .filter((c) => c.body.secret_version === 2);
    expect(rotated).toHaveLength(1);
    expect(kubeconfigs(r.root).map((f) => path.basename(f))).toEqual([KUBECONFIG_FILE]);
    // The rotated credential got a fresh, proven grant for the same attempt before it was written.
    expect(r.route!.opened.map(([who]) => who)).toEqual([
      { taskId: TASK, attempt: 1 },
      { taskId: TASK, attempt: 1 },
    ]);
  });

  it("removes the kubeconfig when a rotation no longer gets a proven route", async () => {
    let intercepted = false;
    const r = await run(
      "anthropic",
      "ready",
      recordingRoute(() =>
        intercepted
          ? { ok: false, reason: "env_integration_gateway_intercepts", error: "intercepted" }
          : null,
      ),
    );
    await until(() => r.inbound.length > 0, 8000, "the task prompt");
    intercepted = true;
    r.w.state.version = 2;
    await until(() => kubeconfigs(r.root).length === 0, 8000, "the kubeconfig is removed");
  });

  it("sweeps a previous process's kubeconfigs from the group directory before claiming", async () => {
    // The real group directory (`groups/<folder>` under the cwd), not the workspace seam.
    const cwd = mkdtempSync(path.join(os.tmpdir(), "argus-integrations-cwd-"));
    tmp.push(cwd);
    const stale = path.join(cwd, "groups", "argus-worker", TASK_INTEGRATIONS_DIR, "other", "1-x");
    mkdirSync(stale, { recursive: true, mode: 0o700 });
    writeFileSync(path.join(stale, "kubeconfig"), TOKEN);
    const spy = vi.spyOn(process, "cwd").mockReturnValue(cwd);
    try {
      const r = await run();
      await until(() => r.w.calls.some((c) => c.path === "/tasks/claim"), 8000, "the claim");
      expect(readdirSync(path.join(cwd, "groups", "argus-worker", TASK_INTEGRATIONS_DIR))).toEqual(
        [],
      );
    } finally {
      spy.mockRestore();
    }
  });

  it("removes the kubeconfig when Argus ends the claim", async () => {
    const r = await run();
    await until(() => r.inbound.length > 0, 8000, "the task prompt");
    r.w.state.rejectHeartbeat = true;
    await until(() => r.recycleRuntime.mock.calls.length > 0, 8000, "the session stops");
    await until(() => !existsSync(r.root), 8000, "the kubeconfig is removed");
    expect(r.route!.revoked).toContainEqual([TASK, undefined]);
  });

  it.each([
    ["anthropic", "expired", "env_integration_unavailable"],
    ["anthropic", "store_unavailable", "env_integration_unavailable"],
    ["openai", "ready", "env_integration_unsupported"],
  ])(
    "a %s engine with a %s integration blocks with %s before the model starts",
    async (provider, readiness, reason) => {
      const r = await run(provider, readiness);
      await until(() => r.posts("/block").length > 0, 8000, "the block");
      expect(r.posts("/block")[0]!.body).toMatchObject({ attempt: 1, reason });
      expect(r.inbound).toHaveLength(0);
      expect(existsSync(r.root)).toBe(false);
      expect(r.posts("/integrations/int-1/credential")).toHaveLength(0);
    },
  );

  it.each([
    [
      "env_integration_gateway_intercepts",
      "staging: the certificate presented through the route is not trusted by the integration's CA (SELF_SIGNED_CERT_IN_CHAIN); something intercepts TLS or the CA is wrong",
    ],
    [
      "env_integration_route_unavailable",
      "this worker has no docker bridge address for the Kubernetes route",
    ],
  ] as const)("blocks with %s before the model, and keeps no kubeconfig", async (reason, error) => {
    const route = recordingRoute(() => ({ ok: false, reason, error }));
    const r = await run("anthropic", "ready", route);
    await until(() => r.posts("/block").length > 0, 8000, "the block");
    const block = r.posts("/block")[0]!.body;
    expect(block).toMatchObject({ attempt: 1, reason });
    expect(JSON.stringify(block)).toContain(error);
    expect(r.inbound).toHaveLength(0);
    expect(existsSync(r.root)).toBe(false);
    // The route was asked for this task and attempt, with the cluster alone.
    expect(route.opened).toEqual([
      [
        { taskId: TASK, attempt: 1 },
        [
          {
            integration_id: "int-1",
            name: "staging",
            server: "https://k8s.example.test:6443",
            ca_data: null,
            authorize: expect.any(Function),
          },
        ],
      ],
    ]);
    expect(route.revoked).toContainEqual([TASK, undefined]);
    expect(JSON.stringify(r.w.calls.filter((c) => c.path.endsWith("/block")))).not.toContain(TOKEN);
    expect(r.logs.join("\n")).not.toContain(TOKEN);
  });

  it("blocks when no route is configured", async () => {
    const r = await run("anthropic", "ready", null);
    await until(() => r.posts("/block").length > 0, 8000, "the block");
    expect(r.posts("/block")[0]!.body).toMatchObject({
      reason: "env_integration_route_unavailable",
    });
    expect(r.inbound).toHaveLength(0);
    expect(existsSync(r.root)).toBe(false);
  });

  it("points the session at an MCP grant, never the token, and removes it when the attempt ends", async () => {
    const mcp = recordingMcp();
    const r = await run("anthropic", "ready", null, mcp);
    await until(() => r.inbound.length > 0, 8000, "the task prompt");
    // The broker holds the token; the session config and the prompt only the grant.
    expect(mcp.tokens).toEqual([`${TOKEN}-v1`]);
    expect(mcp.sessions[mcp.sessions.length - 1]).toEqual({
      "argus-integration-staging": { type: "http", url: GRANT },
    });
    const prompt = r.inbound[0].content.text as string;
    expect(prompt).toContain("mcp__argus-integration-staging__search");
    expect(prompt).not.toContain(TOKEN);
    expect(prompt).not.toContain(GRANT);
    await until(() => mcp.renewed() > 0, 8000, "a heartbeat renews the grant");

    r.w.state.readiness = "revoked";
    await until(() => mcp.revoked.length > 0, 8000, "the grant is revoked");
    expect(mcp.sessions[mcp.sessions.length - 1]).toEqual({});
    expect(r.posts("/complete")).toHaveLength(0);
    expect(r.logs.join("\n")).not.toContain(TOKEN);
    expect(r.logs.join("\n")).not.toContain(GRANT);
  });

  it("blocks an MCP integration when no broker is configured", async () => {
    const r = await run("anthropic", "ready", null, undefined, "mcp");
    await until(() => r.posts("/block").length > 0, 8000, "the block");
    expect(r.posts("/block")[0]!.body).toMatchObject({
      reason: "env_integration_route_unavailable",
    });
  });
});
