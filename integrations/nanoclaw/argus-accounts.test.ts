import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  appendFileSync,
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
import { fileURLToPath } from "node:url";
import Database from "better-sqlite3";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { onecliPathMatches } from "../gateway-providers/onecli-path-match.js";
import { OUTBOUND_SCHEMA } from "../mailbox/sqlite/schema.js";
import type { ChannelSetup } from "./adapter.js";
import { accountsFromEnv, closeSessionFromEnv, createArgusAdapter, renderTask } from "./argus.js";
import {
  type AccountLease,
  applyLease,
  applyLeases,
  applyLeasesDetailed,
  applyRepositoryLease,
  assertOneCliUrl,
  assignmentConfirmed,
  CURSOR_EXPIRY_MARGIN_MS,
  classifyFailure,
  classifyLogTail,
  classifyProviderFailure,
  copilotProblem,
  exchangeCursorApiKey,
  executorFirst,
  executorFor,
  jwtExpiryMs,
  OneCliClient,
  preferredExecutorForLease,
  releaseRepositoryLease,
  runtimeProviders,
  sanitizeNotice,
  verifyRepositoryAssignment,
} from "./argus-accounts.js";
import { seedClaudeContinuation } from "./argus-quota-resume.js";
import { sessionRepositoryProbe } from "./argus-repo-preflight.js";

// NanoClaw's own modules are replaced, so the real `recycleRuntime` runs against a
// fake registry of containers, not NanoClaw's database or a real container runtime.
const nanoclaw = vi.hoisted(() => ({
  running: new Set<string>(),
  killed: [] as string[],
  exits: new Map<string, () => void>(),
  sessions: [] as { id: string; thread_id?: string | null; status?: string }[],
}));
vi.mock("../db/messaging-groups.js", () => ({
  getMessagingGroupByPlatform: async () => ({ id: "mg-1" }),
  getMessagingGroupAgents: async () => [{ agent_group_id: "group-1" }],
}));
vi.mock("../db/agent-groups.js", () => ({
  getAgentGroup: async (id: string) => ({ id, name: "argus worker", folder: "argus-worker" }),
}));
vi.mock("../db/sessions.js", () => ({
  getSessionsByAgentGroup: async (id: string) => (id === "group-1" ? nanoclaw.sessions : []),
  findSessionForAgent: async (group: string, mg: string, thread: string | null) =>
    group === "group-1" && mg === "mg-1"
      ? nanoclaw.sessions.find((s) => (s.thread_id ?? null) === thread && s.status === "active")
      : undefined,
  updateSession: async (id: string, updates: { status?: string }) => {
    const session = nanoclaw.sessions.find((s) => s.id === id);
    if (session) Object.assign(session, updates);
  },
}));
// A local lease would bind the real Docker bridge (172.17.0.1:PORT), colliding with a host's own
// forward; the forwarder has its own test (argus-local-forward.test.ts).
vi.mock("./argus-local-forward.js", () => ({ syncLocalStrataForward: async () => undefined }));
vi.mock("../container-runner.js", () => ({
  isContainerRunning: (id: string) => nanoclaw.running.has(id),
  killContainer: (id: string, _reason: string, onExit?: () => void) => {
    nanoclaw.killed.push(id);
    if (onExit) nanoclaw.exits.set(id, onExit);
  },
}));

describe("assertOneCliUrl", () => {
  const userNet = { dockerNetwork: "onecli-control" };

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("accepts the default host.docker.internal URL", () => {
    expect(assertOneCliUrl("http://host.docker.internal:10254")).toBe(
      "http://host.docker.internal:10254",
    );
  });

  it("accepts http://onecli:10254 on a user-defined Docker network", () => {
    expect(assertOneCliUrl("http://onecli:10254", userNet)).toBe("http://onecli:10254");
    expect(assertOneCliUrl("http://onecli:10254/", userNet)).toBe("http://onecli:10254/");
    vi.stubEnv("ARGUS_NANOCLAW_NETWORK", "onecli-control");
    expect(assertOneCliUrl("http://onecli:10254")).toBe("http://onecli:10254");
    expect(() => new OneCliClient("http://onecli:10254", fetch)).not.toThrow();
  });

  it.each(["", "bridge", "BRIDGE", "default", "host", "none"])(
    "rejects http://onecli:10254 with reserved or missing docker network (%s)",
    (network) => {
      expect(() => assertOneCliUrl("http://onecli:10254", { dockerNetwork: network })).toThrow(
        /user-defined Docker network/,
      );
    },
  );

  it("rejects http://onecli:10254 when ARGUS_NANOCLAW_NETWORK is unset", () => {
    expect(() => assertOneCliUrl("http://onecli:10254")).toThrow(/user-defined Docker network/);
  });

  it.each([
    "http://onecli.example:10254",
    "http://onecli.evil:10254",
    "http://evilonecli:10254",
    "http://onecli:10255",
    "http://onecli:10254/extra",
    "http://onecli:10254?x=1",
    "http://onecli:10254#frag",
    "http://user@onecli:10254",
    "http://onecli:abc",
  ])("rejects private-docker lookalike or malformed URL %s", (url) => {
    expect(() => assertOneCliUrl(url, userNet)).toThrow();
  });

  it("rejects http to a non-allowlisted host", () => {
    expect(() => assertOneCliUrl("http://onecli.example:10254")).toThrow(/https/);
  });

  it("rejects embedded credentials", () => {
    expect(() => assertOneCliUrl("https://user:pass@onecli.example")).toThrow(/credential/);
  });
});

describe("OneCliClient error bodies", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("omits the response body unless ARGUS_ONECLI_DEBUG_BODY=1", async () => {
    const secretBody = '{"error":"vault-secret-xyz","detail":"do-not-leak"}';
    const fetchImpl = (async () => new Response(secretBody, { status: 503 })) as typeof fetch;
    const client = new OneCliClient("http://127.0.0.1:10254", fetchImpl);
    await expect(client.call("GET", "/agents")).rejects.toThrow(/503 vault-secret-xyz/);
    await expect(client.call("GET", "/agents")).rejects.not.toThrow(/do-not-leak/);

    vi.stubEnv("ARGUS_ONECLI_DEBUG_BODY", "1");
    const debugClient = new OneCliClient("http://127.0.0.1:10254", fetchImpl);
    await expect(debugClient.call("GET", "/agents")).rejects.toThrow(/do-not-leak/);
  });
});

describe("recycleRuntime (real hook, NanoClaw modules faked)", () => {
  const recycle = () => {
    vi.stubEnv("ONECLI_URL", "http://127.0.0.1:10254");
    return accountsFromEnv()!.recycleRuntime!;
  };
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
    nanoclaw.running.clear();
    nanoclaw.killed.length = 0;
    nanoclaw.exits.clear();
    nanoclaw.sessions = [];
  });

  it("stops only running sessions and resolves once every one has exited", async () => {
    nanoclaw.sessions = [{ id: "s-run-1" }, { id: "s-idle" }, { id: "s-run-2" }];
    nanoclaw.running.add("s-run-1").add("s-run-2");
    let done = false;
    const pending = recycle()().then(() => {
      done = true;
    });
    await vi.waitFor(() => expect(nanoclaw.exits.size).toBe(2));
    expect(nanoclaw.killed).toEqual(["s-run-1", "s-run-2"]);
    nanoclaw.exits.get("s-run-1")!();
    await Promise.resolve();
    expect(done).toBe(false);
    nanoclaw.exits.get("s-run-2")!();
    await pending;
    expect(done).toBe(true);
  });

  it("resolves without killing anything when no container runs", async () => {
    nanoclaw.sessions = [{ id: "s-idle" }];
    await recycle()();
    expect(nanoclaw.killed).toEqual([]);
  });

  it("rejects when a container never reports its exit", async () => {
    vi.useFakeTimers();
    nanoclaw.sessions = [{ id: "s-stuck" }];
    nanoclaw.running.add("s-stuck");
    const pending = recycle()();
    const outcome = expect(pending).rejects.toThrow("s-stuck did not stop");
    await vi.advanceTimersByTimeAsync(30_000);
    await outcome;
  });
});

// -- fakes ------------------------------------------------------------------------

type Call = { who: "argus" | "onecli"; method: string; path: string; body: any; search?: string };

const lease = (name: string, key: string, over: Partial<AccountLease> = {}): AccountLease => ({
  lease_id: `l-${name}`,
  account_id: `a-${name}`,
  name,
  provider: "anthropic",
  kind: "api_key",
  credential: { api_key: key },
  ...over,
});

describe("repository lease vault mapping", () => {
  it("assigns a generic Authorization secret for only the allowlisted Gitea host and unassigns on release", async () => {
    const w = world({ leases: [] });
    const onecli = new OneCliClient("http://127.0.0.1:10254", w.fetchImpl);
    const target = { identifier: "group-r", name: "repo worker" };
    await applyRepositoryLease(
      onecli,
      target,
      lease("repo", "dummy-gitea-token", {
        provider: "gitea",
        role: "repository",
        credential: { username: "service-user", token: "dummy-gitea-token" },
      }),
      "https://git.example.test/org/project",
      ["https://git.example.test/org/project"],
      [],
    );
    const secretCalls = w.calls.filter(
      (call) => call.path === "/secrets" && call.method === "POST",
    );
    const restSecret = secretCalls.find((call) => call.body.name.endsWith("-rest"))!;
    const gitSecret = secretCalls.find((call) => call.body.name.endsWith("-git"))!;
    expect(restSecret.body).toEqual({
      name: "argus-group-r-repository-rest",
      type: "generic",
      value: "dummy-gitea-token",
      hostPattern: "git.example.test",
      pathPattern: "/api/v1/repos/org/project/*",
      injectionConfig: { headerName: "Authorization", valueFormat: "token {value}" },
    });
    expect(gitSecret.body).toEqual({
      name: "argus-group-r-repository-git",
      type: "generic",
      value: Buffer.from("service-user:dummy-gitea-token").toString("base64"),
      hostPattern: "git.example.test",
      pathPattern: "/org/project.git/*",
      injectionConfig: { headerName: "Authorization", valueFormat: "Basic {value}" },
    });
    expect(JSON.stringify(w.calls.filter((call) => call.who === "argus"))).not.toContain(
      "dummy-gitea-token",
    );
    const before = w.calls.length;
    const check = await verifyRepositoryAssignment(
      onecli,
      target,
      "https://git.example.test/org/project.git",
    );
    expect(check).toMatchObject({
      agent: "found",
      rest: { state: "assigned", scope: "match" },
      git: { state: "assigned", scope: "match" },
    });
    expect(assignmentConfirmed(check)).toBe(true);
    expect(w.calls.slice(before).every((call) => call.method === "GET")).toBe(true);
    // Another repository's patterns are a mismatch, not a confirmation.
    expect(
      assignmentConfirmed(
        await verifyRepositoryAssignment(onecli, target, "https://git.example.test/org/other"),
      ),
    ).toBe(false);
    await releaseRepositoryLease(onecli, target, []);
    expect(w.onecli.agents[0]!.secrets).toEqual([]);
    expect(
      await verifyRepositoryAssignment(onecli, target, "https://git.example.test/org/project"),
    ).toMatchObject({
      rest: { state: "unassigned" },
      git: { state: "unassigned" },
    });
    await expect(
      verifyRepositoryAssignment(
        new OneCliClient(
          "http://127.0.0.1:10254",
          (async () =>
            new Response("Authorization: token dummy-gitea-token", {
              status: 500,
            })) as unknown as typeof fetch,
        ),
        target,
        "https://git.example.test/org/project",
      ),
    ).rejects.toThrow(/^could not read the OneCLI repository assignment$/);
  });
});

/** One fetch for both services, recording calls and holding OneCLI state. */
function world(opts: {
  leases: Array<AccountLease | "no_account">;
  tasks?: any[];
  exhaustedResponse?: (n: number) => any;
  /** Status for `POST /accounts/{id}/auth-rejected` (default 204). */
  authRejectedStatus?: number;
  heartbeatIntervalS?: number;
  rejectHeartbeat?: boolean;
  /** Answer an empty claim after this many ms (an idle long-poll) instead of hanging. */
  idleClaimMs?: number;
}) {
  const calls: Call[] = [];
  const leases = [...opts.leases];
  const tasks = [...(opts.tasks ?? [])];
  let claimedId = "t1";
  const onecli = {
    agents: [] as Array<{
      id: string;
      identifier: string;
      name: string;
      secretMode: string;
      secrets: string[];
    }>,
    secrets: [] as Array<{
      id: string;
      name: string;
      value: string;
      type: string;
      hostPattern: string;
    }>,
  };
  let exhaustedCount = 0;
  const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });

  const fetchImpl = (async (url: string, init: RequestInit) => {
    const u = new URL(url);
    const body = init.body ? JSON.parse(init.body as string) : undefined;
    const method = init.method ?? "GET";
    if (u.hostname === "127.0.0.1") {
      const path = u.pathname.replace(/^\/api/, "");
      calls.push({ who: "onecli", method, path, body });
      if (method === "GET" && path === "/agents") return json(200, onecli.agents);
      if (method === "GET" && path === "/secrets") return json(200, onecli.secrets);
      if (method === "POST" && path === "/agents") {
        const a = { id: `ag-${onecli.agents.length}`, secretMode: "all", secrets: [], ...body };
        onecli.agents.push(a);
        return json(200, a);
      }
      if (method === "POST" && path === "/secrets") {
        const s = { id: `sec-${onecli.secrets.length}`, ...body };
        onecli.secrets.push(s);
        return json(200, s);
      }
      let m = /^\/secrets\/([^/]+)$/.exec(path);
      if (method === "PATCH" && m) {
        const secret = onecli.secrets.find((s) => s.id === m![1])!;
        Object.assign(secret, body);
        return json(200, { success: true });
      }
      m = /^\/agents\/([^/]+)\/secrets$/.exec(path);
      if (method === "GET" && m) {
        return json(200, onecli.agents.find((a) => a.id === m![1])?.secrets ?? []);
      }
      if (method === "PUT" && m) {
        onecli.agents.find((a) => a.id === m![1])!.secrets = body.secretIds;
        return json(200, { success: true });
      }
      m = /^\/agents\/([^/]+)\/secret-mode$/.exec(path);
      if (method === "PATCH" && m) {
        onecli.agents.find((a) => a.id === m![1])!.secretMode = body.mode;
        return json(200, { success: true });
      }
      return json(404, { error: "unknown" });
    }
    const path = u.pathname.replace(/^\/api/, "");
    calls.push({ who: "argus", method, path, body, search: u.search });
    if (path === "/agents/register") {
      return json(201, {
        token: "t",
        agent: { id: "ag" },
        heartbeat_interval_s: opts.heartbeatIntervalS ?? 30,
      });
    }
    if (path === "/agents/me/heartbeat")
      return json(200, {
        state: "active",
        renewed: [],
        rejected: opts.rejectHeartbeat ? [{ task_id: claimedId }] : [],
      });
    if (path === "/agents/me/leave") return json(200, {});
    if (path === "/accounts/lease") {
      // The first queued item for this role (a bare "no_account" serves any role).
      const role = body?.role ?? "engine";
      const index = leases.findIndex((l) => l === "no_account" || (l.role ?? "engine") === role);
      const next = index < 0 ? undefined : leases.splice(index, 1)[0];
      if (next === undefined || next === "no_account") {
        return json(409, { detail: { code: "no_account", message: "no usable account" } });
      }
      return json(200, next);
    }
    if (/^\/accounts\/[^/]+\/exhausted$/.test(path)) {
      const n = ++exhaustedCount;
      if (opts.exhaustedResponse) return json(200, opts.exhaustedResponse(n));
      const next = leases[0];
      if (next && next !== "no_account") {
        return json(200, {
          lease: null,
          next_account_id: next.account_id,
          next_account_name: next.name,
          next_provider: next.provider,
          task_blocked: false,
        });
      }
      return json(200, { lease: null, earliest_reset: null, task_blocked: true });
    }
    if (/^\/accounts\/[^/]+\/auth-rejected$/.test(path)) {
      const status = opts.authRejectedStatus ?? 204;
      if (status === 204) return new Response(null, { status });
      return json(status, { detail: { code: "not_leased", message: "not held" } });
    }
    if (path === "/tasks/claim") {
      const t = tasks.shift();
      if (t) {
        claimedId = t.id;
        return json(200, t);
      }
      if (opts.idleClaimMs !== undefined) {
        await new Promise((r) => setTimeout(r, opts.idleClaimMs));
        return new Response(null, { status: 204 });
      }
      return new Promise<Response>((_, reject) => {
        init.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
      });
    }
    return json(200, {});
  }) as unknown as typeof fetch;

  return { calls, fetchImpl, onecli, leases };
}

const task = () => ({
  id: "t1",
  title: "Write the report",
  spec: "Do it.",
  project: "global",
  required_capabilities: [] as string[],
  attempt: 1,
  acceptance: [],
  knowledge_refs: [],
});

// A repository task gets a workspace keyed by its id, which must be a canonical UUID.
const REPO_ID = "5e1f0c2a-7b3d-4e8f-9a6b-1c2d3e4f5a6b";
const repoTask = () => ({ ...task(), id: REPO_ID, required_capabilities: ["repository:write"] });

const groupDirs: string[] = [];
afterEach(() => {
  for (const d of groupDirs.splice(0)) rmSync(d, { recursive: true, force: true });
});

function harness(
  w: ReturnType<typeof world>,
  withAccounts = true,
  extra: Partial<NonNullable<Parameters<typeof createArgusAdapter>[0]["accounts"]>> = {},
  top: Partial<Parameters<typeof createArgusAdapter>[0]> = {},
) {
  const inbound: any[] = [];
  // The attempt's thread: replies go back on it, as the session's do.
  let thread: string | null = null;
  const setup = {
    onInbound: vi.fn(async (_p: string, t: string | null, m: any) => {
      thread = t;
      inbound.push(m);
    }),
    onInboundEvent: vi.fn(),
    onMetadata: vi.fn(),
    onAction: vi.fn(),
  } as unknown as ChannelSetup;
  const groupDir = mkdtempSync(path.join(os.tmpdir(), "argus-group-"));
  groupDirs.push(groupDir);
  const adapter = createArgusAdapter({
    url: "http://argus.test",
    bootstrapToken: "boot",
    capabilities: [],
    name: "w",
    fetch: w.fetchImpl,
    claimWaitS: 1,
    probeRepository: async () => {}, // offline: no OneCLI gateway
    workspaceRoot: async () => groupDir,
    ...top,
    accounts: withAccounts
      ? {
          models: true,
          onecli: new OneCliClient("http://127.0.0.1:10254", w.fetchImpl),
          resolveTarget: async () => ({ identifier: "ag-group-1", name: "Worker" }),
          ...extra,
        }
      : undefined,
  });
  return { adapter, setup, inbound, thread: () => thread, groupDir };
}

const isRepositoryRelease = (c: Call) =>
  c.path === "/accounts/release" && c.search === "?role=repository";

const until = async (cond: () => boolean, ms = 3000) => {
  const start = Date.now();
  while (!cond()) {
    if (Date.now() - start > ms) throw new Error("timed out waiting for condition");
    await new Promise((r) => setTimeout(r, 5));
  }
};

const live: Array<{ teardown: () => Promise<void> }> = [];
afterEach(async () => {
  for (const a of live.splice(0)) await a.teardown();
});

const failure = (text: string) => ({ kind: "chat", content: { text, failureNotice: true } });

// -- classification and mapping -------------------------------------------------------

describe("anthropic credential documents", () => {
  const apply = async (credential: Record<string, unknown>) => {
    const w = world({ leases: [] });
    await applyLease(
      new OneCliClient("http://127.0.0.1:10254", w.fetchImpl),
      { identifier: "g", name: "G" },
      lease("x", "", { credential }),
    );
    return w.onecli.secrets;
  };

  it("accepts Claude Code's claudeAiOauth.accessToken", async () => {
    const secrets = await apply({
      claudeAiOauth: { accessToken: "tok-123", expiresAt: Date.now() + 3_600_000 },
    });
    expect(secrets.map((s) => s.value)).toEqual(["tok-123"]);
  });

  it("prefers a flat api_key over the nested document", async () => {
    expect((await apply({ api_key: "k", claudeAiOauth: { accessToken: "t" } }))[0]?.value).toBe(
      "k",
    );
  });

  it("refuses an expired access token without echoing any value", async () => {
    const err = await apply({
      claudeAiOauth: { accessToken: "tok-secret", expiresAt: Date.now() - 1000 },
    }).catch((e) => e);
    expect(String(err.message)).toMatch(/expired anthropic access token/);
    expect(String(err.message)).not.toMatch(/tok-secret/);
  });

  it("an expiry beside a flat token does not matter, and ISO dates work", async () => {
    expect(
      (await apply({ oauth_token: "sk-ant-oat01-long-lived", claudeAiOauth: { expiresAt: 1 } }))[0]
        ?.value,
    ).toBe("sk-ant-oat01-long-lived");
    const err = await apply({
      claudeAiOauth: { accessToken: "t", expiresAt: "2001-01-01T00:00:00Z" },
    }).catch((e) => e);
    expect(String(err.message)).toMatch(/expired/);
  });

  it("refuses an oauth_token or token that is not an sk-ant-oat token, naming no value", async () => {
    const fake = `${"A".repeat(48)}#${"B".repeat(43)}`;
    for (const credential of [{ oauth_token: fake }, { token: fake }]) {
      const err = await apply(credential).catch((e) => e);
      expect(String(err.message)).toMatch(/not a token.*renew it in Argus/);
      expect(String(err.message)).not.toContain(fake);
    }
    expect((await apply({ oauth_token: "sk-ant-oat01-fake" }))[0]?.value).toBe("sk-ant-oat01-fake");
  });

  it("still reports a document with no usable field", async () => {
    const err = await apply({ claudeAiOauth: { refreshToken: "r" } }).catch((e) => e);
    expect(String(err.message)).toMatch(/no usable credential field/);
  });
});

describe("classifyFailure", () => {
  it("recognizes the provider's blocked-run messages and the reset time", () => {
    expect(
      classifyFailure("Rate limit [five_hour] (resets 2026-10-15T01:00:00.000Z)"),
    ).toMatchObject({
      kind: "exhausted",
      code: "quota_exhausted",
      source: "notice",
      reason: "Rate limit [five_hour] (resets 2026-10-15T01:00:00.000Z)",
      until: "2026-10-15T01:00:00.000Z",
      resetStatus: "stated",
      resetText: "2026-10-15T01:00:00.000Z",
    });
    const credits = classifyFailure("Out of credits");
    expect(credits).toMatchObject({
      kind: "exhausted",
      code: "quota_exhausted",
      source: "notice",
      resetStatus: "absent",
    });
    expect(credits).not.toHaveProperty("until");
  });
  it("recognizes Claude's session-limit wording and the UTC clock reset", () => {
    const now = new Date("2026-10-20T20:00:00.000Z");
    const text = "You've hit your session limit · resets 11:50pm (UTC)";
    expect(classifyFailure(text, "anthropic", now)).toMatchObject({
      kind: "exhausted",
      code: "quota_exhausted",
      source: "notice",
      reason: text,
      until: "2026-10-20T23:50:00.000Z",
      resetStatus: "stated",
      resetText: "11:50pm (UTC)",
    });
    expect(
      classifyFailure("You\u2019ve hit your limit · resets 11:50 pm (UTC)", "anthropic", now),
    ).toMatchObject({ kind: "exhausted", until: "2026-10-20T23:50:00.000Z" });
    // already past today: the next occurrence is tomorrow
    expect(
      classifyFailure("You've hit your session limit · resets 1:05am (UTC)", "anthropic", now),
    ).toMatchObject({ until: "2026-10-21T01:05:00.000Z" });
    const usage = classifyFailure("Usage limit reached", "anthropic", now);
    expect(usage).toMatchObject({ kind: "exhausted", resetStatus: "absent" });
    expect(usage).not.toHaveProperty("until");
    // Words but no time with a zone: kept as the provider's text, never a guessed time.
    const vague = classifyFailure("You've hit your limit · resets sometime", "anthropic", now);
    expect(vague).toMatchObject({
      kind: "exhausted",
      code: "quota_exhausted",
      resetStatus: "ambiguous",
      resetText: "sometime",
    });
    expect(vague).not.toHaveProperty("until");
    expect(
      classifyFailure("Rate limit [five_hour] (resets 2026-10-20T23:50:00.000Z)", "anthropic"),
    ).toMatchObject({ until: "2026-10-20T23:50:00.000Z" });
    // another provider's account is not exhausted by Claude's wording
    expect(classifyFailure(text, "openai", now).kind).toBe("failed");
  });
  it("treats everything else as a plain failure, anchored to the start", () => {
    expect(classifyFailure("Connection reset by peer (ECONNRESET)", "anthropic").kind).toBe(
      "failed",
    );
    expect(classifyFailure("The agent run failed. Check the logs for details.").kind).toBe(
      "failed",
    );
    expect(classifyFailure("Failed to authenticate. API Error: 401").kind).toBe("failed");
    expect(classifyFailure("the tool hit a rate limit in some API").kind).toBe("failed");
  });
});

describe("applyLease", () => {
  it("creates the agent and a selective secret, then updates it in place on a switch", async () => {
    const w = world({ leases: [] });
    const onecli = new OneCliClient("http://127.0.0.1:10254", w.fetchImpl);
    const target = { identifier: "ag-group-1", name: "Worker" };

    await applyLease(onecli, target, lease("a", "sk-ant-AAAA"));
    expect(w.onecli.secrets).toHaveLength(1);
    expect(w.onecli.secrets[0]).toMatchObject({
      name: "argus-ag-group-1",
      type: "anthropic",
      hostPattern: "api.anthropic.com",
      value: "sk-ant-AAAA",
    });
    expect(w.onecli.agents[0]).toMatchObject({ identifier: "ag-group-1", secretMode: "selective" });
    expect(w.onecli.agents[0]!.secrets).toEqual([w.onecli.secrets[0]!.id]);

    await applyLease(onecli, target, lease("b", "sk-ant-BBBB"));
    expect(w.onecli.secrets).toHaveLength(1); // same secret, new value
    expect(w.onecli.secrets[0]!.value).toBe("sk-ant-BBBB");
    expect(w.onecli.agents).toHaveLength(1);
  });

  it("uses an OAuth token for subscription accounts", async () => {
    const w = world({ leases: [] });
    await applyLease(
      new OneCliClient("http://127.0.0.1:10254", w.fetchImpl),
      { identifier: "g", name: "n" },
      lease("sub", "", { kind: "subscription", credential: { oauth_token: "sk-ant-oat01-XYZ" } }),
    );
    expect(w.onecli.secrets[0]!.value).toBe("sk-ant-oat01-XYZ");
  });

  it("rejects unknown providers and credentials with no usable field, without echoing values", async () => {
    const w = world({ leases: [] });
    const onecli = new OneCliClient("http://127.0.0.1:10254", w.fetchImpl);
    await expect(
      applyLease(onecli, { identifier: "g", name: "n" }, lease("x", "k", { provider: "mystery" })),
    ).rejects.toThrow(/cannot be applied/);
    await expect(
      applyLease(
        onecli,
        { identifier: "g", name: "n" },
        lease("x", "", { credential: { other: "SECRETVALUE" } }),
      ),
    ).rejects.toThrow(/no usable credential field/);
    await expect(
      applyLease(
        onecli,
        { identifier: "g", name: "n" },
        lease("x", "", { credential: { other: "SECRETVALUE" } }),
      ),
    ).rejects.not.toThrow(/SECRETVALUE/);
  });
});

// -- the channel -----------------------------------------------------------------------

describe("channel with accounts", () => {
  it("claims a task before leasing and applying its account", async () => {
    const w = world({ leases: [lease("a", "sk-ant-AAAA")], tasks: [task()] });
    const h = harness(w);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    const order = w.calls.map((c) => `${c.who}:${c.path}`);
    expect(order.indexOf("argus:/tasks/claim")).toBeLessThan(
      order.indexOf("argus:/accounts/lease"),
    );
    expect(order.indexOf("argus:/tasks/claim")).toBeLessThan(order.indexOf("onecli:/agents"));
    expect(w.onecli.secrets[0]!.value).toBe("sk-ant-AAAA");
  });

  it("claims with the providers it can apply and leases a restricted task on the local account", async () => {
    const restricted = { ...task(), account_providers: ["local"] };
    const w = world({ leases: [lease("a", "sk-ant-AAAA")], tasks: [restricted] });
    const h = harness(w);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    const claim = w.calls.find((c) => c.path === "/tasks/claim")!;
    expect(claim.body).toMatchObject({
      runtime_providers: ["anthropic", "openai", "local", "custom"],
    });
    // the restriction is the server's: the lease request is unchanged and names the task
    const leaseCall = w.calls.find((c) => c.path === "/accounts/lease")!;
    expect(leaseCall.body).toMatchObject({
      runtime_providers: ["anthropic", "openai", "local", "custom"],
      task_id: "t1",
      attempt: 1,
    });
  });

  it("applies the engine lease reserved by the claim instead of leasing again", async () => {
    const reserved = { ...lease("a", "sk-ant-AAAA"), role: "engine" };
    const w = world({ leases: [], tasks: [{ ...task(), engine_lease: reserved }] });
    const h = harness(w);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    const engineLeases = w.calls.filter(
      (c) => c.path === "/accounts/lease" && ((c.body as any)?.role ?? "engine") === "engine",
    );
    expect(engineLeases).toHaveLength(0);
    expect(w.onecli.secrets[0]!.value).toBe("sk-ant-AAAA");
  });

  it("gives a claimed task back as quota while no account is usable, and starts once one is", async () => {
    // Leases are bound to a claim, so the channel claims first; Argus only hands it a
    // task some agent can serve and parks the returned one until an account frees up.
    const w = world({
      leases: ["no_account", lease("a", "sk-ant-AAAA")],
      tasks: [task(), { ...task(), attempt: 2 }],
    });
    const h = harness(w);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => w.calls.some((c) => c.path === "/tasks/t1/fail"), 7000); // the retry sleeps 5 s
    const fail = w.calls.find((c) => c.path === "/tasks/t1/fail")!;
    expect(fail.body).toMatchObject({ attempt: 1, reason: "quota" });
    await until(() => h.inbound.length === 1, 7000);
    // attempt 1 never reached the agent; the re-offered attempt 2 did
    expect(h.inbound.map((m) => m.id)).toEqual(["argus-t1-2"]);
  }, 15_000);

  it("on a rate-limit failure: reports the account, switches, and re-sends the task", async () => {
    const w = world({
      leases: [lease("a", "sk-ant-AAAA"), lease("b", "sk-ant-BBBB")],
      tasks: [task()],
    });
    const h = harness(w);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);

    await h.adapter.deliver(
      "local",
      h.thread(),
      failure("Rate limit [five_hour] (resets 2026-10-15T01:00:00.000Z)"),
    );
    const report = w.calls.find((c) => c.path === "/accounts/a-a/exhausted")!;
    expect(report.body).toMatchObject({
      until: "2026-10-15T01:00:00.000Z",
      task_id: "t1",
      attempt: 1,
    });
    expect(w.onecli.secrets[0]!.value).toBe("sk-ant-BBBB"); // switched in the vault
    await until(() => h.inbound.length === 2);
    expect(h.inbound[1].content.text).toContain("account was switched");
    expect(h.inbound[1].content.text).toContain("Write the report");
    expect(w.calls.some((c) => c.path.endsWith("/fail"))).toBe(false); // still the same attempt

    await h.adapter.deliver("local", h.thread(), { kind: "chat", content: "COMPLETE: done" });
    expect(w.calls.some((c) => c.path === "/tasks/t1/complete")).toBe(true);
  });

  it("with no replacement: Argus blocks the task and the channel lets go of it", async () => {
    const w = world({ leases: [lease("a", "sk-ant-AAAA")], tasks: [task()] });
    const h = harness(w);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", h.thread(), failure("Out of credits"));
    expect(w.calls.some((c) => c.path === "/accounts/a-a/exhausted")).toBe(true);
    expect(w.calls.some((c) => c.path.endsWith("/fail"))).toBe(false); // Argus blocked it
    // Anything the agent says now is not for a task we hold.
    const before = w.calls.length;
    await h.adapter.deliver("local", h.thread(), { kind: "chat", content: "COMPLETE: late" });
    expect(w.calls.slice(before).some((c) => c.path.endsWith("/complete"))).toBe(false);
  });

  it("a Claude session limit with nothing to switch to fails the attempt as quota", async () => {
    const w = world({
      leases: [lease("a", "sk-ant-AAAA")],
      tasks: [task()],
      exhaustedResponse: () => ({ lease: null, next_account_id: null, task_blocked: false }),
    });
    const h = harness(w);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver(
      "local",
      h.thread(),
      failure("You've hit your session limit · resets 11:50pm (UTC)"),
    );
    expect(w.calls.some((c) => c.path === "/accounts/a-a/exhausted")).toBe(true);
    const fail = w.calls.find((c) => c.path === "/tasks/t1/fail")!;
    expect(fail.body).toMatchObject({ attempt: 1, reason: "quota" });
  });

  it("gives the account back when it cannot be applied to the vault", async () => {
    const w = world({ leases: [lease("a", "sk-ant-AAAA")], tasks: [task()] });
    const failing = ((url: string, init: RequestInit) =>
      new URL(url).hostname === "127.0.0.1"
        ? Promise.resolve(new Response('{"error":"down"}', { status: 503 }))
        : w.fetchImpl(url, init)) as typeof fetch;
    const inbound: any[] = [];
    const adapter = createArgusAdapter({
      url: "http://argus.test",
      bootstrapToken: "boot",
      capabilities: [],
      name: "w",
      fetch: failing,
      claimWaitS: 1,
      accounts: {
        models: true,
        onecli: new OneCliClient("http://127.0.0.1:10254", failing),
        resolveTarget: async () => ({ identifier: "g", name: "n" }),
      },
    });
    live.push(adapter);
    await adapter.setup({
      onInbound: async (_p: string, _t: string | null, m: any) => void inbound.push(m),
      onInboundEvent: () => {},
      onMetadata: () => {},
      onAction: () => {},
    } as unknown as ChannelSetup);
    await until(() => w.calls.some((c) => c.path === "/accounts/release"));
    // a lease or apply error (not a provider limit) goes back to Argus as an
    // unspent admission failure, never to the agent
    await until(() => w.calls.some((c) => c.path === "/tasks/t1/fail"), 7000);
    expect(w.calls.find((c) => c.path === "/tasks/t1/fail")!.body).toMatchObject({
      reason: "account_unavailable",
    });
    expect(inbound).toHaveLength(0);
  }, 10_000);

  it("a non-quota failure notice fails the attempt with its text", async () => {
    const w = world({ leases: [lease("a", "sk-ant-AAAA")], tasks: [task()] });
    const h = harness(w);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", h.thread(), failure("Failed to authenticate. API Error: 401"));
    const fail = w.calls.find((c) => c.path === "/tasks/t1/fail")!;
    expect(fail.body).toEqual({
      attempt: 1,
      error: "Failed to authenticate. API Error: 401",
      reason: "provider_failure",
    });
    expect(w.calls.some((c) => c.path.endsWith("/exhausted"))).toBe(false);
  });

  it("without accounts configured: no leasing, no vault calls; failures still fail the attempt", async () => {
    const w = world({ leases: [], tasks: [task()] });
    const h = harness(w, false);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", h.thread(), failure("Rate limit"));
    expect(w.calls.some((c) => c.who === "onecli" || c.path.startsWith("/accounts"))).toBe(false);
    expect(w.calls.find((c) => c.path === "/tasks/t1/fail")!.body.error).toBe("Rate limit");
  });
});

// -- codex accounts (docs/specs/codex-accounts.spec.md) --------------------------------

const chatgpt = {
  auth_mode: "chatgpt",
  OPENAI_API_KEY: null,
  tokens: {
    id_token: "id-SECRET",
    access_token: "access-SECRET",
    refresh_token: "refresh-SECRET",
    account_id: "acct",
  },
  last_refresh: "2026-01-01T00:00:00Z",
};
const codexLease = (over: Partial<AccountLease> = {}) =>
  lease("codex-1", "", { provider: "openai", kind: "subscription", credential: chatgpt, ...over });

describe("preferredExecutorForLease", () => {
  const eng = (provider: string) => lease("e", "", { provider, credential: {} });
  const exe = (provider: string) => lease("x", "", { provider, role: "executor", credential: {} });
  // The executor lease actually held decides; engine-only harnesses have no CLI of their own.
  it.each([
    ["custom", "anthropic", "claude"],
    ["custom", "openai", "codex"],
    ["custom", "cursor", "agent"],
    ["custom", null, null],
    ["local", "openai", "codex"],
    ["local", "cursor", "agent"],
    ["local", "local", "opencode"], // local as executor runs through OpenCode
    ["local", "anthropic", null], // the delegate refuses claude on the local model
    ["local", null, null],
    ["anthropic", "local", "opencode"],
    ["anthropic", null, "claude"], // native reuse: the engine's credential serves its CLI
    ["openai", null, "codex"],
    ["anthropic", "cursor", "agent"],
    ["openai", "anthropic", "claude"],
  ] as const)("engine %s + executor %s -> %s", (engine, executor, expected) => {
    expect(preferredExecutorForLease(eng(engine), executor ? exe(executor) : null)).toBe(expected);
  });
  it("is null without any lease", () => {
    expect(preferredExecutorForLease(null, null)).toBeNull();
  });
});

describe("openai (Codex) credential documents", () => {
  const target = { identifier: "g", name: "G" };

  it("gives OneCLI the whole ChatGPT auth.json as an openai secret for chatgpt.com", async () => {
    const w = world({ leases: [] });
    const engine = await applyLease(
      new OneCliClient("http://127.0.0.1:10254", w.fetchImpl),
      target,
      codexLease(),
    );
    expect(engine).toBe("codex");
    const [secret] = w.onecli.secrets;
    expect(secret).toMatchObject({
      name: "argus-g-openai",
      type: "openai",
      hostPattern: "chatgpt.com",
    });
    expect(JSON.parse(secret!.value)).toEqual(chatgpt);
  });

  it("maps an API-key document to api.openai.com", async () => {
    const w = world({ leases: [] });
    await applyLease(
      new OneCliClient("http://127.0.0.1:10254", w.fetchImpl),
      target,
      codexLease({ kind: "api_key", credential: { auth_mode: "apikey", OPENAI_API_KEY: "sk-1" } }),
    );
    expect(w.onecli.secrets[0]).toMatchObject({ value: "sk-1", hostPattern: "api.openai.com" });
  });

  it("refuses an incomplete login without echoing a value", async () => {
    const w = world({ leases: [] });
    const bad = { auth_mode: "chatgpt", tokens: { access_token: "access-SECRET" } };
    let message = "";
    try {
      await applyLease(
        new OneCliClient("http://127.0.0.1:10254", w.fetchImpl),
        target,
        codexLease({ credential: bad }),
      );
    } catch (e) {
      message = (e as Error).message;
    }
    expect(message).toMatch(/no usable credential/);
    expect(message).not.toContain("SECRET");
    expect(w.onecli.secrets).toEqual([]);
  });

  it("assigns exactly the secrets of the leases held", async () => {
    const w = world({ leases: [] });
    const onecli = new OneCliClient("http://127.0.0.1:10254", w.fetchImpl);
    const claude = lease("claude-1", "sk-ant", { role: "executor" });
    expect(await applyLeases(onecli, target, [codexLease(), claude])).toBe("codex");
    const agent = w.onecli.agents[0]!;
    const byId = (id: string) => w.onecli.secrets.find((s) => s.id === id)!.name;
    expect(agent.secrets.map(byId).sort()).toEqual(["argus-g", "argus-g-openai"]);
    expect(agent.secretMode).toBe("selective");
    // The executor lease ends: its secret stays stored but is no longer assigned.
    await applyLeases(onecli, target, [codexLease()]);
    expect(agent.secrets.map(byId)).toEqual(["argus-g-openai"]);
    // Same provider twice (one account in both roles): one secret, the first wins.
    await applyLeases(onecli, target, [lease("a", "k1"), lease("b", "k2", { role: "executor" })]);
    expect(agent.secrets.map(byId)).toEqual(["argus-g"]);
    expect(w.onecli.secrets.find((s) => s.name === "argus-g")!.value).toBe("k1");
  });
});

describe("engine and executor leases in the channel", () => {
  it("leases an anthropic executor next to a Codex engine and selects the harness", async () => {
    const w = world({
      leases: [codexLease(), lease("claude-1", "sk-ant", { role: "executor" })],
      tasks: [task()],
    });
    const selectEngine = vi.fn(async () => true);
    const { adapter, setup } = harness(w, true, {
      selectEngine,
    });
    live.push(adapter);
    await adapter.setup(setup);
    await until(() => w.calls.some((c) => c.path === "/engines/selected"));
    const leaseCalls = w.calls.filter((c) => c.path === "/accounts/lease").map((c) => c.body);
    expect(leaseCalls).toEqual([
      { runtime_providers: ["anthropic", "openai", "local", "custom"], task_id: "t1", attempt: 1 },
      {
        role: "executor",
        runtime_providers: ["anthropic", "openai", "local", "custom", "cursor", "copilot"],
        reuse_held: true,
        task_id: "t1",
        attempt: 1,
      },
    ]);
    expect(JSON.stringify(leaseCalls)).not.toContain('"providers"');
    expect(selectEngine).toHaveBeenCalledWith("codex");
    const selected = w.calls.find((c) => c.path === "/engines/selected")!.body;
    // task_id/attempt close the failover trail (account-selection.spec.md 3.6).
    expect(selected).toEqual({
      engine: "codex",
      provider: "openai",
      account_id: "a-codex-1",
      reason: "lease",
      task_id: "t1",
      attempt: 1,
    });
    expect(JSON.stringify(w.calls.filter((c) => c.who === "argus"))).not.toContain("SECRET");
    expect(w.onecli.agents[0]!.secrets).toHaveLength(2);
  });

  it("holds no executor lease when the best executor is the engine's own provider", async () => {
    const w = world({
      leases: [lease("claude-1", "sk-ant"), lease("claude-2", "sk-ant2", { role: "executor" })],
      tasks: [task()],
    });
    const selectEngine = vi.fn(async () => false);
    const { adapter, setup } = harness(w, true, {
      selectEngine,
    });
    live.push(adapter);
    await adapter.setup(setup);
    await until(() => w.calls.some((c) => c.path === "/engines/selected"));
    expect(w.calls.filter((c) => c.path === "/accounts/lease")).toHaveLength(2);
    expect(
      w.calls.some((c) => c.path === "/accounts/release" && c.search === "?role=executor"),
    ).toBe(true);
    expect(selectEngine).toHaveBeenCalledWith("claude");
    expect(w.onecli.agents[0]!.secrets).toHaveLength(1);
  });

  it("still serves Codex when no executor account is usable, and holds no residual secret", async () => {
    const w = world({ leases: [codexLease(), "no_account"], tasks: [task()] });
    const { adapter, setup } = harness(w, true);
    live.push(adapter);
    await adapter.setup(setup);
    await until(() => w.calls.some((c) => c.path === "/engines/selected"));
    const agent = w.onecli.agents[0]!;
    expect(agent.secrets).toHaveLength(1);
    expect(w.onecli.secrets.find((s) => s.id === agent.secrets[0])!.name).toBe(
      "argus-ag-group-1-openai",
    );
    expect(w.calls.some((c) => c.path === "/accounts/release")).toBe(true);
  });
});

describe("an unusable executor account", () => {
  it("is dropped without taking the Codex engine down", async () => {
    const expired = lease("claude-old", "", {
      role: "executor",
      credential: { claudeAiOauth: { accessToken: "t-SECRET", expiresAt: Date.now() - 1000 } },
    });
    const w = world({ leases: [codexLease(), expired], tasks: [task()] });
    const { adapter, setup } = harness(w, true);
    live.push(adapter);
    await adapter.setup(setup);
    await until(() => w.calls.some((c) => c.path === "/engines/selected"));
    const agent = w.onecli.agents[0]!;
    expect(agent.secrets).toHaveLength(1);
    expect(w.onecli.secrets.find((s) => s.id === agent.secrets[0])!.name).toMatch(/-openai$/);
    expect(w.calls.some((c) => c.path === "/accounts/release")).toBe(true);
    expect(JSON.stringify(w.calls)).not.toContain("t-SECRET");
  });
});

describe("classifyFailure for Codex and by provider", () => {
  it.each([
    "HTTP 401 token_expired",
    "Error: token_expired",
    "401 unauthorized authentication failed",
  ])("treats %j as auth expiry for an openai engine", (text) => {
    expect(classifyFailure(text, "openai").kind).toBe("exhausted");
  });

  it.each(["401 Unauthorized", "HTTP 401 unauthorized"])(
    "treats %j as auth expiry for a custom engine",
    (text) => {
      expect(classifyFailure(text, "custom").kind).toBe("exhausted");
    },
  );

  it.each([
    "usage_limit_reached",
    "You've hit your usage limit. Try again later.",
    "You\u2019ve hit your usage limit",
    "insufficient_quota: You exceeded your current quota",
    "Reconnecting... 2/5: usage_limit_reached",
    "Error: insufficient_quota",
  ])("treats %j as a quota end for an openai engine", (text) => {
    expect(classifyFailure(text, "openai").kind).toBe("exhausted");
    expect(classifyFailure(text).kind).toBe("exhausted");
  });

  it("does not take innocent text or mid-sentence mentions for quota", () => {
    for (const text of [
      "I checked the docs: the API returns usage_limit_reached when full",
      "Progress: the quota page says insufficient_quota is a 429",
      "Reconnecting... 2/5: selected workspace missing from routing discovery",
      "The agent run failed. Check the logs for details.",
    ]) {
      expect(classifyFailure(text, "openai").kind).toBe("failed");
    }
  });

  it("counts only the engine provider's own messages", () => {
    expect(classifyFailure("Rate limit [five_hour]", "openai").kind).toBe("failed");
    expect(classifyFailure("Out of credits", "openai").kind).toBe("failed");
    expect(classifyFailure("usage_limit_reached", "anthropic").kind).toBe("failed");
    expect(classifyFailure("Rate limit [five_hour]", "anthropic").kind).toBe("exhausted");
  });
});

// -- cursor accounts (docs/specs/cursor-accounts.spec.md) ------------------------------

function cursorJwt(expSec: number) {
  const payload = Buffer.from(JSON.stringify({ sub: "auth0|fake", exp: expSec })).toString(
    "base64url",
  );
  return `h.${payload}.fakesig`;
}

const FUTURE_SEC = Math.floor(Date.now() / 1000) + 86400 * 30;
const CURSOR_ACCESS = cursorJwt(FUTURE_SEC);
const CURSOR_REFRESH = cursorJwt(FUTURE_SEC + 1);
const CURSOR_API_KEY = `key_${"FAKE".repeat(10)}`;
const CURSOR_LOGIN = { accessToken: CURSOR_ACCESS, refreshToken: CURSOR_REFRESH };
const CURSOR_SETTINGS = {
  version: 1,
  permissions: { allow: [], deny: [] },
  editor: { vimMode: false },
  model: { modelId: "composer-2.5" },
  authInfo: { authId: "auth0|SETTINGS", email: "x@example.test" },
};

const cursorLease = (over: Partial<AccountLease> = {}) =>
  lease("cursor-1", "", {
    provider: "cursor",
    kind: "subscription",
    credential: CURSOR_LOGIN,
    role: "executor",
    ...over,
  });

describe("cursor credential documents", () => {
  const target = { identifier: "g", name: "G" };

  it("maps an OAuth login to two generic secrets on api2.cursor.sh only", async () => {
    const w = world({ leases: [] });
    const onecli = new OneCliClient("http://127.0.0.1:10254", w.fetchImpl);
    await applyLeases(onecli, target, [codexLease(), cursorLease()]);
    const posts = w.calls.filter((c) => c.path === "/secrets" && c.method === "POST");
    const agent = posts.find((c) => c.body.name === "argus-g-cursor-agent")!;
    const ai = posts.find((c) => c.body.name === "argus-g-cursor-aiserver")!;
    expect(agent.body).toMatchObject({
      type: "generic",
      hostPattern: "api2.cursor.sh",
      pathPattern: "/agent.v1.*",
      injectionConfig: { headerName: "Authorization", valueFormat: "Bearer {value}" },
      value: CURSOR_ACCESS,
    });
    expect(ai.body).toMatchObject({
      hostPattern: "api2.cursor.sh",
      pathPattern: "/aiserver.v1.*",
      value: CURSOR_ACCESS,
    });
    expect(posts.some((c) => c.body.hostPattern === "evil.cursor.sh")).toBe(false);
    expect(JSON.stringify(w.calls)).not.toContain(CURSOR_REFRESH);
  });

  it("exchanges an API key on the trusted host and stores the access token only", async () => {
    const exchanged = cursorJwt(FUTURE_SEC + 60);
    const w = world({ leases: [] });
    const fetchImpl = (async (url: string, init?: RequestInit) => {
      if (url === "https://api2.cursor.sh/auth/exchange_user_api_key") {
        expect(init?.headers && (init.headers as Record<string, string>).Authorization).toBe(
          `Bearer ${CURSOR_API_KEY}`,
        );
        return new Response(JSON.stringify({ accessToken: exchanged }), { status: 200 });
      }
      return w.fetchImpl(url, init as RequestInit);
    }) as typeof fetch;
    const onecli = new OneCliClient("http://127.0.0.1:10254", fetchImpl);
    await applyLeases(
      onecli,
      target,
      [
        codexLease(),
        cursorLease({
          kind: "api_key",
          credential: { api_key: CURSOR_API_KEY },
        }),
      ],
      fetchImpl,
    );
    const agent = w.onecli.secrets.find((s) => s.name === "argus-g-cursor-agent")!;
    expect(agent.value).toBe(exchanged);
    expect(w.calls.some((c) => c.who === "argus")).toBe(false);
  });

  it("rejects settings-only and expired documents without echoing secrets", async () => {
    const w = world({ leases: [] });
    const onecli = new OneCliClient("http://127.0.0.1:10254", w.fetchImpl);
    for (const credential of [
      CURSOR_SETTINGS,
      { accessToken: cursorJwt(Math.floor(Date.now() / 1000) - 60), refreshToken: CURSOR_REFRESH },
      {
        accessToken: cursorJwt(Math.floor((Date.now() + CURSOR_EXPIRY_MARGIN_MS) / 1000)),
        refreshToken: CURSOR_REFRESH,
      },
    ]) {
      const err = await applyLeases(onecli, target, [
        codexLease(),
        cursorLease({ credential }),
      ]).catch((e) => e);
      expect(String(err.message)).toMatch(/cursor-1/);
      expect(String(err.message)).not.toContain(CURSOR_ACCESS);
      expect(String(err.message)).not.toContain(CURSOR_API_KEY);
    }
    expect(w.onecli.secrets).toEqual([]);
  });

  it("unassigns cursor secrets when the executor lease ends but keeps the engine", async () => {
    const w = world({ leases: [] });
    const onecli = new OneCliClient("http://127.0.0.1:10254", w.fetchImpl);
    await applyLeases(onecli, target, [codexLease(), cursorLease()]);
    const names = w.onecli.agents[0]!.secrets.map(
      (id) => w.onecli.secrets.find((s) => s.id === id)!.name,
    );
    expect(names.sort()).toEqual(
      ["argus-g-cursor-agent", "argus-g-cursor-aiserver", "argus-g-openai"].sort(),
    );
    await applyLeases(onecli, target, [codexLease()]);
    const after = w.onecli.agents[0]!.secrets.map(
      (id) => w.onecli.secrets.find((s) => s.id === id)!.name,
    );
    expect(after).toEqual(["argus-g-openai"]);
    expect(w.onecli.secrets.some((s) => s.name.includes("cursor"))).toBe(true);
  });
});

// -- copilot accounts (docs/specs/copilot-accounts.spec.md) ----------------------------

// Fake tokens only: the filler says so, and no request leaves the test.
const COPILOT_TOKEN = `github_pat_FAKE${"0".repeat(20)}_NOT_A_REAL_TOKEN`;
const copilotLease = (over: Partial<AccountLease> = {}) =>
  lease("copilot-1", "", {
    provider: "copilot",
    kind: "subscription",
    credential: { token: COPILOT_TOKEN },
    role: "executor",
    ...over,
  });

/** Whether a set of generic secrets would inject on `https://host/path`, as the gateway matches. */
const injects = (secrets: Array<Record<string, unknown>>, host: string, urlPath: string) =>
  secrets.some(
    (s) => s.hostPattern === host && onecliPathMatches(urlPath, String(s.pathPattern ?? "")),
  );

describe("copilot credential documents", () => {
  const target = { identifier: "g", name: "G" };

  it("is an executor-only provider run by the copilot delegate", () => {
    expect(runtimeProviders("engine")).not.toContain("copilot");
    expect(runtimeProviders("executor")).toContain("copilot");
    expect(executorFor("copilot")).toBe("copilot");
    expect(preferredExecutorForLease(codexLease(), copilotLease())).toBe("copilot");
  });

  it("maps the token to exactly the probed hosts and paths, one secret each", async () => {
    const w = world({ leases: [] });
    const onecli = new OneCliClient("http://127.0.0.1:10254", w.fetchImpl);
    await applyLeases(onecli, target, [codexLease(), copilotLease()]);
    const posts = w.calls
      .filter((c) => c.path === "/secrets" && c.method === "POST")
      .map((c) => c.body)
      .filter((b) => String(b.name).startsWith("argus-g-copilot-"));
    expect(posts.map((b) => [b.name, b.hostPattern, b.pathPattern])).toEqual([
      ["argus-g-copilot-user", "api.github.com", "/copilot_internal/user"],
      ["argus-g-copilot-models", "api.individual.githubcopilot.com", "/models"],
      ["argus-g-copilot-responses", "api.individual.githubcopilot.com", "/responses"],
      ["argus-g-copilot-chat", "api.individual.githubcopilot.com", "/chat/completions"],
      ["argus-g-copilot-messages", "api.individual.githubcopilot.com", "/v1/messages"],
    ]);
    for (const b of posts) {
      expect(b).toMatchObject({
        type: "generic",
        value: COPILOT_TOKEN,
        injectionConfig: { headerName: "Authorization", valueFormat: "Bearer {value}" },
      });
      expect(String(b.pathPattern)).not.toContain("*");
    }
    for (const [host, urlPath] of [
      ["api.github.com", "/copilot_internal/user"],
      ["api.individual.githubcopilot.com", "/models"],
      ["api.individual.githubcopilot.com", "/responses"],
      ["api.individual.githubcopilot.com", "/chat/completions"],
      ["api.individual.githubcopilot.com", "/v1/messages"],
    ] as const)
      expect(injects(posts, host, urlPath)).toBe(true);
    for (const [host, urlPath] of [
      ["api.business.githubcopilot.com", "/chat/completions"],
      ["api.enterprise.githubcopilot.com", "/models"],
      ["telemetry.individual.githubcopilot.com", "/telemetry"],
      ["api.github.com", "/repos/acme/widget"],
      ["api.github.com", "/copilot_internal/managed_settings"],
      ["api.github.com", "/copilot_internal/user/extra"],
      ["api.github.com", "/user"],
      ["api.individual.githubcopilot.com", "/mcp"],
      ["api.individual.githubcopilot.com", "/models/session"],
      ["github.com", "/github/copilot-cli/releases"],
    ] as const)
      expect(injects(posts, host, urlPath), `${host}${urlPath}`).toBe(false);
  });

  it("never widens a GitHub repository secret on the shared api.github.com host", async () => {
    const w = world({ leases: [] });
    const onecli = new OneCliClient("http://127.0.0.1:10254", w.fetchImpl);
    const repoLease = lease("gh", "", {
      provider: "github",
      role: "repository",
      credential: { token: "github_pat_FAKE_REPOSITORY_NOT_A_REAL_TOKEN" },
    });
    await applyRepositoryLease(
      onecli,
      target,
      repoLease,
      "https://github.com/acme/widget",
      ["https://github.com/acme/widget"],
      [],
    );
    const before = JSON.stringify(w.onecli.secrets.filter((s) => s.name.includes("-repository-")));
    await applyLeases(onecli, target, [codexLease(), copilotLease(), repoLease]);
    const repo = w.onecli.secrets.filter((s) => s.name.includes("-repository-"));
    expect(JSON.stringify(repo)).toBe(before);
    const rest = repo.find((s) => s.name.endsWith("-rest"))!;
    expect((rest as Record<string, unknown>).pathPattern).toBe("/repos/acme/widget/*");
    expect(injects(repo, "api.github.com", "/copilot_internal/user")).toBe(false);
    const copilot = w.onecli.secrets.filter((s) => s.name.startsWith("argus-g-copilot-"));
    expect(injects(copilot, "api.github.com", "/repos/acme/widget/pulls")).toBe(false);
    // The repository secrets stay assigned next to the Copilot ones.
    const assigned = w.onecli.agents[0]!.secrets.map(
      (id) => w.onecli.secrets.find((s) => s.id === id)!.name,
    );
    expect(assigned).toEqual(
      expect.arrayContaining(["argus-g-repository-rest", "argus-g-copilot-user"]),
    );
  });

  it("re-applies idempotently and unassigns every copilot secret on release", async () => {
    const w = world({ leases: [] });
    const onecli = new OneCliClient("http://127.0.0.1:10254", w.fetchImpl);
    await applyLeases(onecli, target, [codexLease(), copilotLease()]);
    await applyLeases(onecli, target, [codexLease(), copilotLease()]);
    expect(w.onecli.secrets.filter((s) => s.name.startsWith("argus-g-copilot-"))).toHaveLength(5);
    await applyLeases(onecli, target, [codexLease()]);
    const after = w.onecli.agents[0]!.secrets.map(
      (id) => w.onecli.secrets.find((s) => s.id === id)!.name,
    );
    expect(after).toEqual(["argus-g-openai"]);
  });

  it.each([
    [{ token: `ghp_${"F".repeat(36)}` }, /classic personal access token \(ghp_\)/],
    [{ token: `gho_FAKE`, extra: "x" }, /unexpected fields: extra/],
    [{ token: "FAKE_UNPREFIXED_TOKEN" }, /must start with one of/],
    [{ token: "github_pat_" }, /must start with one of/],
    [{ token: "gho_FAKE TOKEN" }, /without whitespace/],
    [{}, /non-empty string/],
  ])("rejects %j without echoing the value", async (credential, message) => {
    expect(copilotProblem(credential)).toMatch(message);
    const w = world({ leases: [] });
    const onecli = new OneCliClient("http://127.0.0.1:10254", w.fetchImpl);
    const err = await applyLeases(onecli, target, [
      codexLease(),
      copilotLease({ credential }),
    ]).catch((e) => e);
    expect(String(err.message)).toMatch(/copilot-1 \(copilot\)/);
    const value = String((credential as { token?: unknown }).token ?? "");
    const barePrefix = ["github_pat_", "gho_", "ghu_"].includes(value);
    if (value.length > 4 && !barePrefix) expect(String(err.message)).not.toContain(value);
    expect(w.onecli.secrets).toEqual([]);
  });

  it.each(["github_pat_FAKE_x", "gho_FAKE_x", "ghu_FAKE_x"])("accepts %s", (token) => {
    expect(copilotProblem({ token })).toBeNull();
  });

  it("is never an engine lease", async () => {
    const w = world({ leases: [] });
    const onecli = new OneCliClient("http://127.0.0.1:10254", w.fetchImpl);
    await expect(applyLeases(onecli, target, [copilotLease({ role: "engine" })])).rejects.toThrow(
      /executor, not an engine/,
    );
  });
});

describe("exchangeCursorApiKey", () => {
  it("refuses a response without a usable JWT accessToken", async () => {
    const bad = (body: unknown, status = 200) =>
      (async () => new Response(JSON.stringify(body), { status })) as typeof fetch;
    await expect(
      exchangeCursorApiKey(CURSOR_API_KEY, bad({ accessToken: "not-jwt" })),
    ).rejects.toThrow(/no usable accessToken/);
    await expect(
      exchangeCursorApiKey(CURSOR_API_KEY, bad({ accessToken: cursorJwt(1) })),
    ).rejects.toThrow(/no usable accessToken/);
    await expect(exchangeCursorApiKey(CURSOR_API_KEY, bad({}, 401))).rejects.toThrow(/HTTP 401/);
  });
});

describe("cursor executor selection in the channel", () => {
  it("leases cursor for the executor when it is the best usable executor, with no pool list", async () => {
    const w = world({
      leases: [codexLease(), cursorLease()],
      tasks: [task()],
    });
    const { adapter, setup } = harness(w, true);
    live.push(adapter);
    await adapter.setup(setup);
    await until(() => w.calls.some((c) => c.path === "/engines/selected"));
    expect(w.calls.filter((c) => c.path === "/accounts/lease").map((c) => c.body)).toEqual([
      { runtime_providers: ["anthropic", "openai", "local", "custom"], task_id: "t1", attempt: 1 },
      {
        role: "executor",
        runtime_providers: ["anthropic", "openai", "local", "custom", "cursor", "copilot"],
        reuse_held: true,
        task_id: "t1",
        attempt: 1,
      },
    ]);
    expect(w.onecli.secrets.filter((s) => s.name.includes("cursor"))).toHaveLength(2);
  });
});

describe("applyLeasesDetailed", () => {
  it("reports executor JWT expiry for a Cursor executor lease", async () => {
    const w = world({ leases: [] });
    const onecli = new OneCliClient("http://127.0.0.1:10254", w.fetchImpl);
    const exp = jwtExpiryMs(CURSOR_ACCESS);
    const out = await applyLeasesDetailed(onecli, { identifier: "g", name: "G" }, [
      codexLease(),
      cursorLease(),
    ]);
    expect(out.engine).toBe("codex");
    expect(out.executorExpiresAt).toBe(exp);
  });
});

describe("a limit of the executor's provider leaves the Codex engine lease alone", () => {
  it("fails the attempt instead of exhausting the openai account", async () => {
    const w = world({
      leases: [codexLease(), lease("claude-1", "sk-ant", { role: "executor" })],
      tasks: [task()],
    });
    const h = harness(w, true);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    await h.adapter.deliver(
      "local",
      h.thread(),
      failure("Rate limit [five_hour] (resets 2026-10-15T01:00:00.000Z)"),
    );
    await until(() => w.calls.some((c) => /\/tasks\/t1\/fail$/.test(c.path)));
    expect(w.calls.some((c) => /\/accounts\/[^/]+\/exhausted$/.test(c.path))).toBe(false);
  });

  it("reports an openai quota error as exhausted for the engine role", async () => {
    const w = world({ leases: [codexLease(), "no_account"], tasks: [task()] });
    const h = harness(w, true);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    await h.adapter.deliver("local", h.thread(), failure("usage_limit_reached"));
    await until(() => w.calls.some((c) => /\/exhausted$/.test(c.path)));
    const call = w.calls.find((c) => /\/exhausted$/.test(c.path))!;
    expect(call.path).toBe("/accounts/a-codex-1/exhausted");
    expect(call.body).toMatchObject({ role: "engine", reason: "usage_limit_reached" });
  });
});

describe("structured providerFailure on a failure notice", () => {
  const GENERIC = "The agent run failed. Check the logs for details. SECRET-ERR-STRING sk-SYNTH";
  const structured = (providerFailure: unknown, failureNotice = true) => ({
    kind: "chat",
    content: { text: GENERIC, failureNotice, providerFailure },
  });
  const run = async (leases: Array<AccountLease | "no_account">, msg: unknown) => {
    const w = world({ leases, tasks: [task()] });
    const h = harness(w, true);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    await h.adapter.deliver("local", h.thread(), msg as any);
    await until(() =>
      w.calls.some((c) => /\/exhausted$/.test(c.path) || /\/tasks\/t1\/fail$/.test(c.path)),
    );
    return { w, h };
  };
  const exhaustedCalls = (w: ReturnType<typeof world>) =>
    w.calls.filter((c) => /\/exhausted$/.test(c.path));
  const ok = { provider: "codex", kind: "auth_expired" };

  it("exhausts the openai engine lease with a fixed reason, switches and resends", async () => {
    const { w, h } = await run(
      [
        codexLease(),
        "no_account", // executor lease for the first engine
        { ...codexLease(), lease_id: "l-codex-2", account_id: "a-codex-2", name: "codex-2" },
        "no_account", // executor lease for the replacement
      ],
      structured(ok),
    );
    const [call] = exhaustedCalls(w);
    expect(call?.path).toBe("/accounts/a-codex-1/exhausted");
    expect(call?.body).toMatchObject({
      reason: "OpenAI/Codex authentication expired",
      reason_code: "auth_expired",
      source: "structured",
      reset_status: "absent",
      role: "engine",
      runtime_providers: ["anthropic", "openai", "local", "custom"],
      task_id: "t1",
      attempt: 1,
    });
    await until(() => h.inbound.length > 1);
    expect(h.inbound[1].content.text).toContain("account was switched");
    const seen = JSON.stringify([w.calls.filter((c) => c.who === "argus"), h.inbound]);
    expect(seen).not.toContain("SECRET-ERR-STRING");
    expect(seen).not.toContain("sk-SYNTH");
  });

  it("does not exhaust when the held account is not openai", async () => {
    const { w } = await run([lease("claude-1", "sk-ant")], structured(ok));
    expect(exhaustedCalls(w)).toEqual([]);
    expect(w.calls.some((c) => /\/tasks\/t1\/fail$/.test(c.path))).toBe(true);
  });

  it.each([
    ["unknown provider", { provider: "claude", kind: "auth_expired" }],
    ["unknown kind", { provider: "codex", kind: "rate_limited" }],
    ["case variant", { provider: "Codex", kind: "auth_expired" }],
    ["non-string kind", { provider: "codex", kind: 401 }],
    ["string", "auth_expired"],
    ["array", [ok]],
    ["null", null],
    ["empty object", {}],
  ])("fails normally for an invalid value (%s)", async (_n, value) => {
    const { w } = await run([codexLease()], structured(value));
    expect(exhaustedCalls(w)).toEqual([]);
    const fail = w.calls.find((c) => /\/tasks\/t1\/fail$/.test(c.path))!;
    expect(fail.body.error).toContain("The agent run failed");
  });

  it("ignores providerFailure without failureNotice", async () => {
    const w = world({ leases: [codexLease()], tasks: [task()] });
    const h = harness(w, true);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    await h.adapter.deliver("local", h.thread(), structured(ok, false) as any);
    await new Promise((r) => setTimeout(r, 50));
    expect(exhaustedCalls(w)).toEqual([]);
    expect(w.calls.some((c) => /\/tasks\/t1\/fail$/.test(c.path))).toBe(false);
  });

  it("keeps a generic notice without metadata as a plain failure", async () => {
    const { w } = await run([codexLease()], failure("The agent run failed. Check the logs."));
    expect(exhaustedCalls(w)).toEqual([]);
    expect(w.calls.some((c) => /\/tasks\/t1\/fail$/.test(c.path))).toBe(true);
  });

  describe("a structured Claude authentication_failed", () => {
    const claude = { provider: "claude", kind: "auth_expired" };
    const rejectedCalls = (w: ReturnType<typeof world>) =>
      w.calls.filter((c) => /\/auth-rejected$/.test(c.path));
    const failCall = (w: ReturnType<typeof world>, id = "t1") =>
      w.calls.find((c) => c.path === `/tasks/${id}/fail`);

    it("reports a safe auth rejection and fails the attempt without exhausting Claude", async () => {
      const { w, h } = await run([lease("claude-1", "sk-ant-SYNTH-KEY")], structured(claude));
      expect(rejectedCalls(w).map((c) => [c.method, c.path, c.body])).toEqual([
        [
          "POST",
          "/accounts/a-claude-1/auth-rejected",
          { task_id: "t1", attempt: 1, role: "engine", source: "structured" },
        ],
      ]);
      expect(exhaustedCalls(w)).toEqual([]);
      const fail = failCall(w)!;
      expect(fail.body).toMatchObject({ attempt: 1, reason: "provider_failure" });
      expect(fail.body.error).toBe("Claude authentication failed");
      const order = w.calls.map((c) => c.path);
      expect(order.indexOf("/accounts/a-claude-1/auth-rejected")).toBeLessThan(
        order.indexOf("/tasks/t1/fail"),
      );
      const seen = JSON.stringify([w.calls.filter((c) => c.who === "argus"), h.inbound]);
      for (const secret of ["SECRET-ERR-STRING", "sk-SYNTH", "sk-ant-SYNTH-KEY"]) {
        expect(seen).not.toContain(secret);
      }
    });

    it("still fails the attempt when the report is refused", async () => {
      const w = world({
        leases: [lease("claude-1", "sk-ant")],
        tasks: [task()],
        authRejectedStatus: 409,
      });
      const h = harness(w, true);
      live.push(h.adapter);
      await h.adapter.setup(h.setup);
      await until(() => h.inbound.length > 0);
      await h.adapter.deliver("local", h.thread(), structured(claude) as any);
      await until(() => failCall(w) !== undefined);
      expect(rejectedCalls(w)).toHaveLength(1);
      expect(exhaustedCalls(w)).toEqual([]);
      expect(failCall(w)!.body.error).toBe("Claude authentication failed");
    });

    it("needs the held account to be anthropic", async () => {
      const { w } = await run([codexLease(), "no_account"], structured(claude));
      expect(rejectedCalls(w)).toEqual([]);
      expect(exhaustedCalls(w)).toEqual([]);
    });

    it.each([
      ["claude rate_limit", structured({ provider: "claude", kind: "rate_limit" })],
      ["claude billing", structured({ provider: "claude", kind: "billing" })],
      ["claude billing_error", structured({ provider: "claude", kind: "billing_error" })],
      ["claude org", structured({ provider: "claude", kind: "org" })],
      ["claude on_hold", structured({ provider: "claude", kind: "on_hold" })],
      [
        "claude authentication_failed kind",
        structured({ provider: "claude", kind: "authentication_failed" }),
      ],
      ["providerFailure outside a notice", structured(claude, false)],
      ["free-form marker", failure("authentication_failed")],
      [
        "free-form quoted notice",
        failure('Claude said: "authentication_failed: OAuth token revoked"'),
      ],
      [
        "free-form 401",
        failure('Error: 401 {"type":"authentication_error","message":"invalid x-api-key"}'),
      ],
      ["free-form billing", failure("Credit balance is too low")],
      ["free-form org", failure("Your organization has been disabled.")],
      ["free-form on hold", failure("Your account is on hold. Contact support.")],
    ])("%s is not an auth rejection", async (_name, msg) => {
      const w = world({ leases: [lease("claude-1", "sk-ant")], tasks: [task()] });
      const h = harness(w, true);
      live.push(h.adapter);
      await h.adapter.setup(h.setup);
      await until(() => h.inbound.length > 0);
      await h.adapter.deliver("local", h.thread(), msg as any);
      await new Promise((r) => setTimeout(r, 50));
      await until(
        () =>
          !(msg as { content: { failureNotice: boolean } }).content.failureNotice ||
          w.calls.some((c) => /\/exhausted$/.test(c.path) || /\/tasks\/t1\/fail$/.test(c.path)),
      );
      expect(rejectedCalls(w)).toEqual([]);
    });

    it("free-form auth words never exhaust Claude", async () => {
      for (const text of [
        "authentication_failed",
        'Error: 401 {"type":"authentication_error"}',
        "Your account is on hold.",
      ]) {
        const { w } = await run([lease("claude-1", "sk-ant")], failure(text));
        expect(exhaustedCalls(w)).toEqual([]);
        expect(failCall(w)).toBeDefined();
      }
    });

    it("a later ordinary failure does not exhaust Claude", async () => {
      const w = world({
        leases: [lease("claude-1", "sk-ant"), lease("claude-1", "sk-ant")],
        tasks: [task(), { ...task(), id: "t2", title: "Next" }],
      });
      const h = harness(w, true);
      live.push(h.adapter);
      await h.adapter.setup(h.setup);
      await until(() => h.inbound.length > 0);
      await h.adapter.deliver("local", h.thread(), structured(claude) as any);
      await until(() => failCall(w) !== undefined);
      await until(() => h.inbound.length > 1);
      await h.adapter.deliver("local", h.thread(), failure("The agent run failed. Tool crashed."));
      await until(() => failCall(w, "t2") !== undefined);
      expect(rejectedCalls(w)).toHaveLength(1);
      expect(exhaustedCalls(w)).toEqual([]);
      expect(failCall(w, "t2")!.body.error).toContain("Tool crashed");
    });
  });

  it("classifyProviderFailure fails closed without an openai account", () => {
    expect(classifyProviderFailure(ok, undefined)).toBeNull();
    expect(classifyProviderFailure(ok, "anthropic")).toBeNull();
    expect(classifyProviderFailure(ok, "openai")).toMatchObject({
      kind: "exhausted",
      code: "auth_expired",
      source: "structured",
      reason: "OpenAI/Codex authentication expired",
      resetStatus: "absent",
      until: undefined,
    });
  });
});

describe("task-scoped repository auth", () => {
  it.each(["", ".git"])(
    "normalizes configured repository suffix %s for the worker task URL",
    async (suffix) => {
      const repoLease = lease("repo", "dummy-gitea-token", {
        provider: "gitea",
        role: "repository",
        credential: { username: "service-user", token: "dummy-gitea-token" },
      });
      const codeTask = repoTask();
      const w = world({
        leases: [lease("engine", "dummy-engine-key"), repoLease],
        tasks: [codeTask],
      });
      const h = harness(w, true, {
        repositories: { global: `https://git.example.test/org/repo${suffix}` },
      });
      live.push(h.adapter);
      await h.adapter.setup(h.setup);
      await until(() => h.inbound.length > 0);
      expect(h.inbound[0].content.text).toContain(
        "Repository (allowlisted): https://git.example.test/org/repo.git",
      );
      expect(h.inbound[0].content.text).not.toContain("dummy-gitea-token");
      expect(w.calls.filter((c) => c.path === "/accounts/lease").map((c) => c.body)).toEqual([
        {
          runtime_providers: ["anthropic", "openai", "local", "custom"],
          task_id: REPO_ID,
          attempt: 1,
        },
        {
          role: "executor",
          runtime_providers: ["anthropic", "openai", "local", "custom", "cursor", "copilot"],
          reuse_held: true,
          task_id: REPO_ID,
          attempt: 1,
        },
        { role: "repository", runtime_providers: ["gitea"], task_id: REPO_ID, attempt: 1 },
      ]);
      await h.adapter.deliver("local", h.thread(), {
        kind: "chat",
        content: {
          text: `COMPLETE: ${JSON.stringify({
            summary: "PR opened",
            data: {
              delivery: {
                repository: "https://git.example.test/org/repo",
                head_sha: "head-1",
                ci_status: "success",
              },
            },
          })}`,
        },
      });
      const completed = w.calls.find((c) => c.path === `/tasks/${REPO_ID}/complete`)!;
      expect(completed.body.result.data.delivery.head_sha).toBe("head-1");
      await until(() => w.calls.some((c) => isRepositoryRelease(c)));
      expect(w.calls.filter((c) => isRepositoryRelease(c))).toHaveLength(1);
    },
  );

  it("does not acquire repository credentials for non-write global tasks", async () => {
    const w = world({ leases: [lease("engine", "dummy-engine-key")], tasks: [task()] });
    const h = harness(w, true, { repositories: { global: "https://git.example.test/org/repo" } });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    expect(
      w.calls.filter((c) => c.path === "/accounts/lease" && c.body?.role === "repository"),
    ).toHaveLength(0);
    expect(h.inbound[0].content.text).not.toContain("Repository (allowlisted)");
  });

  it.each([
    ["FAIL: cannot continue", `/tasks/${REPO_ID}/fail`],
    ["ASK: need a decision", `/tasks/${REPO_ID}/ask`],
  ])("releases repository access after %s", async (reply, endpoint) => {
    const w = world({
      leases: [
        lease("engine", "dummy-engine-key"),
        lease("repo", "dummy-gitea-token", {
          provider: "gitea",
          role: "repository",
          credential: { username: "service-user", token: "dummy-gitea-token" },
        }),
      ],
      tasks: [repoTask()],
    });
    const h = harness(w, true, { repositories: { global: "https://git.example.test/org/repo" } });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    await h.adapter.deliver("local", h.thread(), { kind: "chat", content: { text: reply } });
    await until(() => w.calls.some((call) => isRepositoryRelease(call)));
    expect(w.calls.some((call) => call.path === endpoint)).toBe(true);
  });

  it("releases repository access when heartbeat reports lease loss", async () => {
    const w = world({
      leases: [
        lease("engine", "dummy-engine-key"),
        lease("repo", "dummy-gitea-token", {
          provider: "gitea",
          role: "repository",
          credential: { username: "service-user", token: "dummy-gitea-token" },
        }),
      ],
      tasks: [repoTask()],
      heartbeatIntervalS: 1,
      rejectHeartbeat: true,
    });
    const h = harness(w, true, { repositories: { global: "https://git.example.test/org/repo" } });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    await until(() => w.calls.some((call) => isRepositoryRelease(call)));
  });

  it("stops the session when Argus releases the claim, and reports nothing", async () => {
    const w = world({
      leases: [lease("engine", "dummy-engine-key")],
      tasks: [task()],
      heartbeatIntervalS: 1,
      rejectHeartbeat: true,
    });
    const recycleRuntime = vi.fn(async () => undefined);
    const h = harness(w, true, { recycleRuntime });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    await until(() => recycleRuntime.mock.calls.length > 0, 3000);
    // Model-trace lifecycle posts on claim are fine; a released claim must not
    // complete, fail, ask, progress or block.
    expect(
      w.calls.some(
        (call) =>
          call.path.startsWith("/tasks/t1/") &&
          !call.path.endsWith("/models") &&
          !call.path.endsWith("/observations"),
      ),
    ).toBe(false);
  });

  it("fails closed before requesting Gitea access without a known base lease set", async () => {
    const w = world({
      leases: [],
      tasks: [{ ...task(), required_capabilities: ["repository:write"] }],
    });
    const h = harness(w, true, {
      models: false,
      repositories: { global: "https://git.example.test/org/repo" },
    });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => w.calls.some((call) => call.path === "/tasks/t1/block"));
    expect(w.calls.find((call) => call.path === "/tasks/t1/block")!.body).toMatchObject({
      reason: "env_repository_unavailable",
    });
    expect(w.calls.some((call) => call.path === "/tasks/t1/fail")).toBe(false);
    expect(w.calls.some((call) => call.path === "/accounts/lease")).toBe(false);
    expect(h.inbound).toHaveLength(0);
  });

  const repoWorld = () =>
    world({
      leases: [
        lease("engine", "dummy-engine-key"),
        lease("repo", "dummy-gitea-token", {
          provider: "gitea",
          role: "repository",
          credential: { username: "service-user", token: "dummy-gitea-token" },
        }),
      ],
      tasks: [repoTask()],
    });

  it("blocks a task whose project has no repository before any model work", async () => {
    const w = repoWorld();
    const h = harness(w, true, { repositories: {} });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => w.calls.some((call) => call.path === `/tasks/${REPO_ID}/block`));
    expect(w.calls.find((call) => call.path === `/tasks/${REPO_ID}/block`)!.body).toMatchObject({
      reason: "env_repository_unconfigured",
      error: "no repository is configured for project global",
    });
    expect(h.inbound).toHaveLength(0);
  });

  it("blocks a task whose repository does not answer, and releases its access", async () => {
    const w = repoWorld();
    const h = harness(
      w,
      true,
      { repositories: { global: "https://git.example.test/org/repo" } },
      {
        probeRepository: async () => {
          throw new Error("Gitea API GET failed with HTTP 502");
        },
      },
    );
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => w.calls.some((call) => call.path === `/tasks/${REPO_ID}/block`));
    expect(w.calls.find((call) => call.path === `/tasks/${REPO_ID}/block`)!.body).toMatchObject({
      reason: "env_repository_unreachable",
      error: "repository preflight session_failure: Gitea API GET failed with HTTP 502",
    });
    await until(() => w.calls.some((call) => isRepositoryRelease(call)));
    expect(w.calls.some((call) => call.path === `/tasks/${REPO_ID}/fail`)).toBe(false);
    expect(h.inbound).toHaveLength(0);
  });

  it("runs the preflight with the applied lease's provider and exact allowlist before the task is injected", async () => {
    const w = repoWorld();
    const seen: Array<{
      url: string;
      allowlist: readonly string[];
      provider: string;
      injected: number;
    }> = [];
    const h = harness(
      w,
      true,
      { repositories: { global: "https://git.example.test/org/repo" } },
      {
        probeRepository: async (url, allowlist, provider) => {
          seen.push({ url, allowlist, provider, injected: h.inbound.length });
        },
      },
    );
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    expect(seen).toEqual([
      {
        url: "https://git.example.test/org/repo.git",
        allowlist: ["https://git.example.test/org/repo"],
        provider: "gitea",
        injected: 0,
      },
    ]);
  });

  it("fails closed when no preflight session is configured", async () => {
    const w = repoWorld();
    const h = harness(
      w,
      true,
      { repositories: { global: "https://git.example.test/org/repo" } },
      { probeRepository: undefined },
    );
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => w.calls.some((call) => call.path === `/tasks/${REPO_ID}/block`));
    expect(w.calls.find((call) => call.path === `/tasks/${REPO_ID}/block`)!.body).toMatchObject({
      reason: "env_repository_unreachable",
      error: "repository preflight session_failure: no repository preflight session is configured",
    });
    await until(() => w.calls.some((call) => isRepositoryRelease(call)));
    expect(w.calls.some((call) => call.path === `/tasks/${REPO_ID}/fail`)).toBe(false);
    expect(h.inbound).toHaveLength(0);
  });

  it("blocks as unavailable, before any forge request, when OneCLI does not show the assignment", async () => {
    const w = repoWorld();
    const fetchImpl = w.fetchImpl;
    // OneCLI accepted the apply but its agent's assignment list does not show it.
    w.fetchImpl = (async (url: string, init: RequestInit) =>
      (init.method ?? "GET") === "GET" && /\/api\/agents\/[^/]+\/secrets$/.test(url)
        ? new Response("[]", { status: 200 })
        : fetchImpl(url, init)) as unknown as typeof fetch;
    const probeRepository = vi.fn(async () => {});
    const h = harness(
      w,
      true,
      { repositories: { global: "https://git.example.test/org/repo" } },
      { probeRepository },
    );
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => w.calls.some((call) => call.path === `/tasks/${REPO_ID}/block`));
    expect(w.calls.find((call) => call.path === `/tasks/${REPO_ID}/block`)!.body).toMatchObject({
      reason: "env_repository_unavailable",
      error: expect.stringMatching(/^repository preflight assignment_missing: /),
    });
    expect(probeRepository).not.toHaveBeenCalled();
    await until(() => w.calls.some((call) => isRepositoryRelease(call)));
    expect(h.inbound).toHaveLength(0);
  });

  it("posts one attempt-correlated, secret-free evidence line to the task log", async () => {
    const w = repoWorld();
    const h = harness(
      w,
      true,
      { repositories: { global: "https://git.example.test/org/repo" } },
      {
        probeRepository: async () => {
          throw new Error("proxy said http://agent:dummy-gitea-token@proxy:10255 refused");
        },
      },
    );
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => w.calls.some((call) => call.path === `/tasks/${REPO_ID}/block`));
    const logs = w.calls.filter((call) => call.path === `/tasks/${REPO_ID}/log`);
    expect(logs).toHaveLength(1);
    const body = logs[0].body as { attempt: number; source: string; lines: string[] };
    expect(body).toMatchObject({ attempt: 1, source: "worker" });
    expect(body.lines).toHaveLength(1);
    const event = JSON.parse(body.lines[0].replace(/^\[repository-preflight\] /, ""));
    expect(event).toMatchObject({
      event: "repository_preflight",
      task_id: REPO_ID,
      attempt: 1,
      provider: "gitea",
      outcome: "session_failure",
      assignment: { read: true, agent: "found", rest: { state: "assigned" } },
    });
    const posted = JSON.stringify(w.calls.filter((call) => call.who === "argus"));
    expect(posted).not.toContain("dummy-gitea-token");
  });

  it("blocks, keeps the task and releases access when the preflight session is denied", async () => {
    const w = repoWorld();
    const run = vi.fn(async () => ({
      exitCode: 13,
      verdict: { ok: false, error: { code: "policy_denied", message: "not authenticated" } },
    }));
    const h = harness(
      w,
      true,
      { repositories: { global: "https://git.example.test/org/repo" } },
      { probeRepository: sessionRepositoryProbe(run) },
    );
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => w.calls.some((call) => call.path === `/tasks/${REPO_ID}/block`));
    expect(run).toHaveBeenCalledWith(
      expect.objectContaining({
        helper: "argus-gitea-api",
        repositoryUrl: "https://git.example.test/org/repo.git",
      }),
    );
    expect(w.calls.find((call) => call.path === `/tasks/${REPO_ID}/block`)!.body).toMatchObject({
      reason: "env_repository_unreachable",
    });
    await until(() => w.calls.some((call) => isRepositoryRelease(call)));
    expect(w.calls.some((call) => call.path === `/tasks/${REPO_ID}/fail`)).toBe(false);
    expect(h.inbound).toHaveLength(0);
  });

  it("blocks a GitHub repository when only a Gitea lease was issued, without any session", async () => {
    const w = repoWorld();
    const run = vi.fn();
    const h = harness(
      w,
      true,
      { repositories: { global: "https://github.com/org/repo" } },
      { probeRepository: sessionRepositoryProbe(run) },
    );
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => w.calls.some((call) => call.path === `/tasks/${REPO_ID}/block`));
    expect(w.calls.find((call) => call.path === `/tasks/${REPO_ID}/block`)!.body).toMatchObject({
      reason: "env_repository_unavailable",
    });
    expect(run).not.toHaveBeenCalled();
    expect(h.inbound).toHaveLength(0);
  });

  it("claims nothing until startup adoption has finished, and claims anyway only when the hook is missing", async () => {
    const claims = (w: ReturnType<typeof repoWorld>) =>
      w.calls.filter((call) => call.path === "/tasks/claim").length;
    let open!: () => void;
    const gate = new Promise<void>((resolve) => {
      open = resolve;
    });
    const w = repoWorld();
    const h = harness(w, true, { repositories: {} }, { startupReady: () => gate });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await new Promise((r) => setTimeout(r, 600));
    expect(w.calls.some((call) => call.path === "/agents/register")).toBe(true);
    expect(claims(w)).toBe(0);
    open();
    await until(() => claims(w) > 0);

    const w2 = repoWorld();
    const h2 = harness(
      w2,
      true,
      { repositories: {} },
      {
        startupReady: async () => {
          throw new Error(
            "container-runner does not export startupAdoptionDone: run install.sh and rebuild NanoClaw",
          );
        },
      },
    );
    live.push(h2.adapter);
    await h2.adapter.setup(h2.setup);
    await until(() => claims(w2) > 0);
  });

  it("does not claim when startup adoption failed", async () => {
    const claimCount = (w: ReturnType<typeof repoWorld>) =>
      w.calls.filter((call) => call.path === "/tasks/claim").length;
    const w = repoWorld();
    const h = harness(
      w,
      true,
      { repositories: {} },
      {
        startupReady: async () => {
          throw new Error("listSessions exploded");
        },
      },
    );
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await new Promise((r) => setTimeout(r, 800));
    expect(claimCount(w)).toBe(0);
  });

  it("a teardown while waiting for startup adoption ends the claim loop without claiming", async () => {
    const w = repoWorld();
    const h = harness(
      w,
      true,
      { repositories: {} },
      { startupReady: () => new Promise(() => undefined) },
    );
    await h.adapter.setup(h.setup);
    await h.adapter.teardown();
    await new Promise((r) => setTimeout(r, 400));
    expect(w.calls.some((call) => call.path === "/tasks/claim")).toBe(false);
  });

  it("never blocks the task or releases its lease while the preflight session may still exist", async () => {
    const w = repoWorld();
    let tornDown = false;
    const order: string[] = [];
    const h = harness(
      w,
      true,
      { repositories: { global: "https://git.example.test/org/repo" } },
      {
        // Slower than the log-only mark below: the session is still being torn down when it fires.
        probeRepository: async () => {
          await new Promise((r) => setTimeout(r, 400));
          tornDown = true;
          order.push("teardown concluded");
          throw new Error(
            "the repository preflight session was not torn down (stop: stop did not finish)",
          );
        },
        repositoryProbeBackstopMs: 50,
      },
    );
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await new Promise((r) => setTimeout(r, 250));
    expect(w.calls.some((call) => call.path === `/tasks/${REPO_ID}/block`)).toBe(false);
    expect(w.calls.some((call) => isRepositoryRelease(call))).toBe(false);
    await until(() => w.calls.some((call) => call.path === `/tasks/${REPO_ID}/block`));
    expect(tornDown).toBe(true);
    expect(w.calls.find((call) => call.path === `/tasks/${REPO_ID}/block`)!.body).toMatchObject({
      reason: "env_repository_unreachable",
    });
  });

  it("redacts a proxy URL's userinfo from the blocked task's error", async () => {
    const w = repoWorld();
    const h = harness(
      w,
      true,
      { repositories: { global: "https://git.example.test/org/repo" } },
      {
        probeRepository: async () => {
          throw new Error(
            "failed: -e HTTPS_PROXY=http://agent:s3cr3t-agent-token@host.docker.internal:10255",
          );
        },
      },
    );
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => w.calls.some((call) => call.path === `/tasks/${REPO_ID}/block`));
    const body = JSON.stringify(
      w.calls.find((call) => call.path === `/tasks/${REPO_ID}/block`)!.body,
    );
    expect(body).not.toContain("s3cr3t-agent-token");
  });

  it("blocks instead of failing when the worker reports a broken environment", async () => {
    const w = repoWorld();
    const h = harness(w, true, { repositories: { global: "https://git.example.test/org/repo" } });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    expect(h.inbound[0].content.text).toContain('"FAIL (environment):"');
    await h.adapter.deliver("local", h.thread(), {
      kind: "chat",
      content: { text: "FAIL (environment): no checkout in /workspace" },
    });
    await until(() => w.calls.some((call) => call.path === `/tasks/${REPO_ID}/block`));
    expect(w.calls.find((call) => call.path === `/tasks/${REPO_ID}/block`)!.body).toMatchObject({
      reason: "worker_env",
      error: "no checkout in /workspace",
    });
    expect(w.calls.some((call) => call.path === `/tasks/${REPO_ID}/fail`)).toBe(false);
  });

  it("prepares the task workspace after the repository preflight and before the model", async () => {
    const order: string[] = [];
    const w = world({
      leases: [
        lease("engine", "dummy-engine-key"),
        lease("repo", "dummy-gitea-token", {
          provider: "gitea",
          role: "repository",
          credential: { username: "service-user", token: "dummy-gitea-token" },
        }),
      ],
      tasks: [
        {
          ...repoTask(),
          // A phase past "implement" makes the continuation preflight run.
          continuation: {
            revision: 1,
            phase: "qa",
            artifacts: [],
            checklist: [],
            checklist_state: "unstructured",
            pending: [],
            decisions: [],
            environment: [],
            reconciliation: [],
            gaps: [],
          },
        },
      ],
    });
    let groupDir = "";
    const h = harness(
      w,
      true,
      { repositories: { global: "https://git.example.test/org/repo" } },
      {
        probeRepository: async () => {
          order.push("repository");
        },
        workspaceRoot: async () => {
          order.push("workspace");
          return groupDir;
        },
        observe: async () => {
          const ws = path.join(groupDir, "task-workspaces", REPO_ID, ".git");
          order.push(existsSync(ws) ? "continuation (workspace ready)" : "continuation");
          return null;
        },
      },
    );
    groupDir = h.groupDir;
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    expect(order).toEqual(["repository", "workspace", "continuation (workspace ready)"]);
    const text = h.inbound[0].content.text as string;
    expect(text).toContain(
      `Workspace: /workspace/agent/task-workspaces/${REPO_ID}, branch argus/task-5e1f0c2a, remote origin https://git.example.test/org/repo.git`,
    );
    expect(text).toContain("do not create another checkout");
  });

  it("blocks with the workspace reason before any model work, and releases its access", async () => {
    const w = repoWorld();
    const h = harness(w, true, { repositories: { global: "https://git.example.test/org/repo" } });
    // A plain directory where the workspace belongs: not a git repository.
    const ws = path.join(h.groupDir, "task-workspaces", REPO_ID);
    mkdirSync(ws, { recursive: true });
    writeFileSync(path.join(ws, "notes.txt"), "work in progress");
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => w.calls.some((call) => call.path === `/tasks/${REPO_ID}/block`));
    expect(w.calls.find((call) => call.path === `/tasks/${REPO_ID}/block`)!.body).toMatchObject({
      reason: "env_workspace_invalid",
      error: "the workspace is not a git repository",
    });
    await until(() => w.calls.some((call) => isRepositoryRelease(call)));
    expect(w.calls.some((call) => call.path === `/tasks/${REPO_ID}/fail`)).toBe(false);
    expect(h.inbound).toHaveLength(0);
    expect(readFileSync(path.join(ws, "notes.txt"), "utf8")).toBe("work in progress");
  });

  it("blocks as unconfigured when the agent group directory is unknown", async () => {
    const w = repoWorld();
    const h = harness(
      w,
      true,
      { repositories: { global: "https://git.example.test/org/repo" } },
      { workspaceRoot: async () => null },
    );
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => w.calls.some((call) => call.path === `/tasks/${REPO_ID}/block`));
    expect(w.calls.find((call) => call.path === `/tasks/${REPO_ID}/block`)!.body).toMatchObject({
      reason: "env_workspace_unconfigured",
    });
    await until(() => w.calls.some((call) => isRepositoryRelease(call)));
    expect(w.calls.some((call) => call.path === `/tasks/${REPO_ID}/fail`)).toBe(false);
    expect(h.inbound).toHaveLength(0);
  });

  // A continuation naming a required input (task inputs spec 3.6) and nothing else.
  const requiringInput = (ref: string) => ({
    revision: 1,
    phase: "implement",
    artifacts: [],
    checklist: [],
    checklist_state: "unstructured",
    pending: [],
    decisions: [],
    environment: [],
    reconciliation: [],
    gaps: [],
    wip: {
      summary: "recovered patch",
      ref,
      provenance: "manager",
      observed_at: "2026-10-30T00:00:00Z",
    },
  });

  // Task inputs spec 3.6: a required input that cannot be had blocks before the workspace.
  it("blocks on a required input it cannot verify, before the workspace and the model", async () => {
    const artifact = "0b6f2f0e-3c1a-4d2b-9e8f-7a6b5c4d3e2f";
    const w = world({
      leases: [
        lease("engine", "dummy-engine-key"),
        lease("repo", "dummy-gitea-token", {
          provider: "gitea",
          role: "repository",
          credential: { username: "service-user", token: "dummy-gitea-token" },
        }),
      ],
      tasks: [
        { ...repoTask(), continuation: requiringInput(`attachment:${artifact}:${"b".repeat(64)}`) },
      ],
    });
    const listing = {
      task_id: REPO_ID,
      inputs: [
        {
          id: artifact,
          task_id: REPO_ID,
          name: "SECRET-NAME.patch",
          media_type: "text/x-diff",
          size: 5,
          sha256: "a".repeat(64),
          attached_by: "orchestrator:o1",
          created_at: "2026-10-30T00:00:00Z",
          receipts: [],
        },
      ],
    };
    const fetchImpl = (async (url: string, init: RequestInit) => {
      if (new URL(url).pathname === `/api/tasks/${REPO_ID}/inputs`) {
        w.calls.push({ who: "argus", method: "GET", path: `/tasks/${REPO_ID}/inputs`, body: null });
        return new Response(JSON.stringify(listing), { status: 200 });
      }
      return w.fetchImpl(url, init);
    }) as unknown as typeof fetch;
    const h = harness(
      w,
      true,
      { repositories: { global: "https://git.example.test/org/repo" } },
      { fetch: fetchImpl },
    );
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => w.calls.some((call) => call.path === `/tasks/${REPO_ID}/block`));
    const block = w.calls.find((call) => call.path === `/tasks/${REPO_ID}/block`)!.body;
    expect(block).toMatchObject({ attempt: 1, reason: "env_input_invalid" });
    expect(block.error).toContain(artifact);
    expect(block.error).not.toContain("SECRET");
    expect(block.error).not.toContain(h.groupDir);
    await until(() => w.calls.some((call) => isRepositoryRelease(call)));
    expect(w.calls.some((call) => call.path.includes("/content"))).toBe(false);
    expect(w.calls.some((call) => call.path === `/tasks/${REPO_ID}/fail`)).toBe(false);
    expect(existsSync(path.join(h.groupDir, "task-workspaces"))).toBe(false);
    expect(h.inbound).toHaveLength(0);
  });

  // Task inputs spec 3.7: another worker of the same task needs no affinity to the first.
  it("lets a second worker on attempt 2 materialize the inputs shared on attempt 1", async () => {
    const id = "7c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f";
    const artifact = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d";
    const bytes = Buffer.from(`diff --git a/x b/x\n+${"café ✓ ".repeat(500)}\n`, "utf8");
    const sha256 = createHash("sha256").update(bytes).digest("hex");
    const listing = {
      task_id: id,
      inputs: [
        {
          id: artifact,
          task_id: id,
          name: "recovered.patch",
          media_type: "text/x-diff",
          size: bytes.length,
          sha256,
          attached_by: "orchestrator:o1",
          created_at: "2026-10-30T00:00:00Z",
          receipts: [],
        },
      ],
    };
    const workerOn = (attempt: number) => {
      const w = world({
        leases: [lease("engine", "dummy-engine-key")],
        tasks: [
          {
            ...task(),
            id,
            attempt,
            continuation: requiringInput(`attachment:${artifact}:${sha256}`),
          },
        ],
      });
      const fetchImpl = (async (url: string, init: RequestInit) => {
        const p = new URL(url).pathname.replace(/^\/api/, "");
        if (p === `/tasks/${id}/inputs` || p === `/artifacts/${artifact}/content`) {
          w.calls.push({ who: "argus", method: "GET", path: p, body: null });
          return p.endsWith("/inputs")
            ? new Response(JSON.stringify(listing), { status: 200 })
            : new Response(bytes, { status: 200 });
        }
        return w.fetchImpl(url, init);
      }) as unknown as typeof fetch;
      const h = harness(w, true, {}, { fetch: fetchImpl, inputsBackoffMs: 0 });
      live.push(h.adapter);
      return { w, h };
    };
    const a = workerOn(1);
    await a.h.adapter.setup(a.h.setup);
    await until(() => a.h.inbound.length > 0);
    await a.h.adapter.teardown();
    const b = workerOn(2);
    expect(b.h.groupDir).not.toBe(a.h.groupDir);
    await b.h.adapter.setup(b.h.setup);
    await until(() => b.h.inbound.length > 0);
    const file = path.join(b.h.groupDir, "task-inputs", id, "recovered.patch");
    expect(readFileSync(file).equals(bytes)).toBe(true);
    expect(b.w.calls.filter((c) => c.path === `/artifacts/${artifact}/content`)).toHaveLength(1);
    const receipts = b.w.calls.filter((c) => c.path === `/tasks/${id}/inputs/${artifact}/receipt`);
    expect(receipts.map((c) => c.body)).toEqual([
      { attempt: 2, state: "materialized", sha256, size: bytes.length },
      { attempt: 2, state: "verified", sha256, size: bytes.length },
    ]);
    const text = b.h.inbound[0].content.text as string;
    expect(text).toContain(
      `- /workspace/agent/task-inputs/${id}/recovered.patch · ${bytes.length} bytes · sha256 ${sha256} · required`,
    );
    expect(text).toContain(`node /workspace/agent/task-inputs/${id}/verify-inputs.mjs`);
    expect(text).not.toContain("café");
    expect(text).not.toContain("argus.test");

    // The verifier ran in the session; `consumed` is relayed while the attempt is still held.
    const verify = spawnSync("node", ["verify-inputs.mjs"], { cwd: path.dirname(file) });
    expect(verify.status).toBe(0);
    await b.h.adapter.deliver("local", b.h.thread(), { kind: "chat", content: "COMPLETE: done" });
    const paths = b.w.calls.map((c) => `${c.path}:${(c.body as { state?: string })?.state ?? ""}`);
    const consumed = paths.indexOf(`/tasks/${id}/inputs/${artifact}/receipt:consumed`);
    expect(consumed).toBeGreaterThan(-1);
    expect(consumed).toBeLessThan(paths.indexOf(`/tasks/${id}/complete:`));
  });

  it("leaves the workspace root alone for a task without repository:write", async () => {
    const w = world({ leases: [lease("engine", "dummy-engine-key")], tasks: [task()] });
    const workspaceRoot = vi.fn(async () => null);
    const h = harness(w, true, {}, { workspaceRoot });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    expect(workspaceRoot).not.toHaveBeenCalled();
    expect(existsSync(path.join(h.groupDir, "task-workspaces"))).toBe(false);
    expect(h.inbound[0].content.text).not.toContain("Workspace:");
  });

  // Task checkpoint spec 3.3: only a completion Argus accepted skips the checkpoint.
  for (const [label, status, saved] of [
    ["a completion Argus refused (409)", 409, true],
    ["an accepted completion", 200, false],
  ] as const) {
    it(`checkpoints the workspace after ${label}: ${saved}`, async () => {
      const w = repoWorld();
      const inner = w.fetchImpl;
      w.fetchImpl = (async (url: string, init: RequestInit) =>
        status !== 200 && new URL(url).pathname.endsWith("/complete")
          ? new Response(
              JSON.stringify({ detail: { code: "stale_attempt", message: "superseded" } }),
              { status },
            )
          : inner(url, init)) as unknown as typeof fetch;
      const store = mkdtempSync(path.join(os.tmpdir(), "argus-ckpt-store-"));
      groupDirs.push(store);
      const h = harness(
        w,
        true,
        { repositories: { global: "https://git.example.test/org/repo" } },
        { checkpointRoot: store },
      );
      live.push(h.adapter);
      await h.adapter.setup(h.setup);
      await until(() => h.inbound.length > 0);
      const ws = path.join(h.groupDir, "task-workspaces", REPO_ID);
      writeFileSync(path.join(ws, "draft.md"), "unpublished work");
      await h.adapter.deliver("local", h.thread(), {
        kind: "chat",
        content: { text: "COMPLETE: done" },
      });
      await until(() => w.calls.filter((c) => c.path === "/tasks/claim").length >= 2);
      const checkpoints = () =>
        existsSync(path.join(store, REPO_ID))
          ? readdirSync(path.join(store, REPO_ID)).filter((n) => /^[0-9]{14}-/.test(n))
          : [];
      // Task finalization spec 3.3: the step is reported only when it ran.
      const steps = () =>
        w.calls
          .filter((c) => c.path === `/tasks/${REPO_ID}/attempts/1/checkpoint`)
          .map((c) => c.body.state);
      if (saved) {
        await until(() => checkpoints().length === 1);
        await until(() => steps().length === 2);
        expect(steps()).toEqual(["running", "saved"]);
      } else {
        expect(checkpoints()).toEqual([]);
        expect(steps()).toEqual([]);
      }
    });
  }
});

describe("runtime recycling on an account rotation", () => {
  const expired = {
    kind: "chat",
    content: {
      text: "The agent run failed.",
      failureNotice: true,
      providerFailure: { provider: "codex", kind: "auth_expired" },
    },
  };
  const codex2 = {
    ...codexLease(),
    lease_id: "l-codex-2",
    account_id: "a-codex-2",
    name: "codex-2",
  };

  it("stops the old runtime before the new account is applied and the task is resent", async () => {
    const w = world({
      leases: [codexLease(), "no_account", codex2, "no_account"],
      tasks: [task()],
    });
    const order: string[] = [];
    const recycleRuntime = vi.fn(async () => {
      order.push(`recycle(inbound=${h.inbound.length}, secret=${w.onecli.secrets.length})`);
    });
    const h = harness(w, true, {
      selectEngine: vi.fn(async () => false), // codex -> codex: the harness does not change
      recycleRuntime,
    });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    expect(recycleRuntime).not.toHaveBeenCalled(); // the initial lease kills nothing
    await h.adapter.deliver("local", h.thread(), expired as any);
    await until(() => h.inbound.length === 2);
    expect(recycleRuntime).toHaveBeenCalledTimes(1);
    // Recycled before the replacement's secret was written (account-selection.spec.md 3.6).
    expect(order[0]).toMatch(/^recycle\(inbound=1, secret=1\)$/);
    expect(h.inbound[1].content.text).toContain("account was switched");
    // Same task and attempt: nothing was failed or retried to renew the session.
    expect(w.calls.some((c) => /\/tasks\/t1\/fail$/.test(c.path))).toBe(false);
    const selected = w.calls.filter((c) => c.path === "/engines/selected").map((c) => c.body);
    expect(selected.map((b) => b.account_id)).toEqual(["a-codex-1", "a-codex-2"]);
  });

  it("also runs on a cross-provider fallback, before the new engine is selected", async () => {
    const w = world({
      leases: [codexLease(), "no_account", lease("claude-1", "sk-ant")],
      tasks: [task()],
    });
    const calls: string[] = [];
    const h = harness(w, true, {
      selectEngine: vi.fn(async (engine: string) => (calls.push(`select:${engine}`), true)),
      recycleRuntime: vi.fn(async () => void calls.push("recycle")),
    });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", h.thread(), expired as any);
    await until(() => h.inbound.length === 2);
    // The old runtime stops before the new account is written and its engine selected.
    expect(calls).toEqual(["select:codex", "recycle", "select:claude"]);
  });

  it("does not recycle when no replacement account exists", async () => {
    const w = world({ leases: [codexLease(), "no_account"], tasks: [task()] });
    const recycleRuntime = vi.fn(async () => undefined);
    const h = harness(w, true, { recycleRuntime });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", h.thread(), expired as any);
    await until(() => w.calls.some((c) => /\/exhausted$/.test(c.path)));
    expect(recycleRuntime).not.toHaveBeenCalled();
  });

  it("does not resend or report success when the old runtime cannot be stopped", async () => {
    const w = world({
      leases: [codexLease(), "no_account", codex2, "no_account"],
      tasks: [task()],
    });
    const recycleRuntime = vi.fn(async () => {
      throw new Error("session s1 did not stop");
    });
    const h = harness(w, true, { recycleRuntime });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", h.thread(), expired as any);
    await until(() => w.calls.some((c) => /\/tasks\/t1\/fail$/.test(c.path)));
    const fail = w.calls.find((c) => /\/tasks\/t1\/fail$/.test(c.path))!;
    expect(fail.body.attempt).toBe(1);
    expect(fail.body.error).toContain("could not be stopped");
    expect(fail.body.reason).toBe("runtime_switch_failed");
    expect(h.inbound).toHaveLength(1); // the old process got nothing more
    expect(w.calls.some((c) => c.path === "/tasks/t1/complete")).toBe(false);
  });
});

describe("global account selection in the channel", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("never sends a provider list or order: Argus ranks every account", async () => {
    const w = world({ leases: [lease("claude-1", "sk-ant")], tasks: [task()] });
    const h = harness(w, true);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => w.calls.some((c) => c.path === "/engines/selected"));
    for (const c of w.calls.filter((c) => c.path === "/accounts/lease"))
      expect(c.body).not.toHaveProperty("providers");
    expect(w.calls.find((c) => c.path === "/engines/selected")!.body).toMatchObject({
      engine: "claude",
      provider: "anthropic",
    });
  });

  it("leases the engine account whatever the provider the best one is", async () => {
    // Argus picked openai (the only usable one): the harness follows the account.
    const w = world({ leases: [codexLease(), "no_account"], tasks: [task()] });
    const selectEngine = vi.fn(async (_engine: string) => true);
    const h = harness(w, true, { selectEngine });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => w.calls.some((c) => c.path === "/engines/selected"));
    expect(selectEngine).toHaveBeenCalledWith("codex");
  });

  it("ignores the retired provider lists in the environment", () => {
    vi.stubEnv("ONECLI_URL", "http://127.0.0.1:10254");
    vi.stubEnv("ARGUS_ACCOUNT_PROVIDERS", "openai");
    vi.stubEnv("ARGUS_EXECUTOR_PROVIDERS", "cursor");
    const accounts = accountsFromEnv()!;
    expect(accounts.models).toBe(true);
    expect(accounts).not.toHaveProperty("providers");
    expect(accounts).not.toHaveProperty("executorProviders");
  });

  it("turns accounts on with a vault URL alone, and stays off without any", () => {
    vi.stubEnv("ONECLI_URL", "");
    vi.stubEnv("ARGUS_ACCOUNT_PROVIDERS", "");
    vi.stubEnv("ARGUS_REPOSITORIES_JSON", "");
    expect(accountsFromEnv()).toBeUndefined();
    vi.stubEnv("ONECLI_URL", "http://127.0.0.1:10254");
    expect(accountsFromEnv()?.models).toBe(true);
  });

  it("leases no model account when models are off (repository-only configuration)", async () => {
    const w = world({ leases: [lease("claude-1", "sk-ant")], tasks: [task()] });
    const h = harness(w, true, { models: false });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    expect(w.calls.some((c) => c.path === "/accounts/lease")).toBe(false);
  });

  it("passes the held engine and executor accounts to the model policy", async () => {
    const w = world({
      leases: [codexLease(), lease("claude-1", "sk-ant", { role: "executor" })],
      tasks: [task()],
    });
    const selectModels = vi.fn(async (_task: unknown, _held: unknown) => ({}) as any);
    const adapter = createArgusAdapter({
      url: "http://argus.test",
      bootstrapToken: "boot",
      capabilities: [],
      name: "w",
      fetch: w.fetchImpl,
      claimWaitS: 1,
      selectModels,
      accounts: {
        models: true,
        onecli: new OneCliClient("http://127.0.0.1:10254", w.fetchImpl),
        resolveTarget: async () => ({ identifier: "ag-group-1", name: "Worker" }),
      },
    });
    live.push(adapter);
    const setup = {
      onInbound: vi.fn(),
      onInboundEvent: vi.fn(),
      onMetadata: vi.fn(),
      onAction: vi.fn(),
    } as unknown as ChannelSetup;
    await adapter.setup(setup);
    await until(() => selectModels.mock.calls.length > 0);
    expect(selectModels.mock.calls[0]![1]).toEqual({
      engine: { provider: "openai", account_id: "a-codex-1" },
      engine_settings: null,
      executor: { provider: "anthropic", account_id: "a-claude-1" },
      executor_settings: null,
    });
  });

  it("keeps the higher-priority engine account as the executor instead of a lower one", async () => {
    // Claude 100 (max_leases 1) is the engine; Argus, asked to reuse held accounts,
    // answers the executor request with that same engine lease, not Codex 0.
    const engine = lease("claude-1", "sk-ant");
    const w = world({ leases: [engine, "no_account"], tasks: [task()] });
    const calls: any[] = [];
    const inner = w.fetchImpl;
    const fetchImpl = (async (url: string, init: RequestInit) => {
      const body = init.body ? JSON.parse(init.body as string) : undefined;
      if (String(url).endsWith("/accounts/lease") && body?.role === "executor") {
        calls.push(body);
        return new Response(JSON.stringify({ ...engine, role: "engine" }), { status: 200 });
      }
      return inner(url, init);
    }) as unknown as typeof fetch;
    const selectEngine = vi.fn(async (_engine: string) => true);
    const h = createArgusAdapter({
      url: "http://argus.test",
      bootstrapToken: "boot",
      capabilities: [],
      name: "w",
      fetch: fetchImpl,
      claimWaitS: 1,
      accounts: {
        models: true,
        onecli: new OneCliClient("http://127.0.0.1:10254", fetchImpl),
        resolveTarget: async () => ({ identifier: "ag-group-1", name: "Worker" }),
        selectEngine,
      },
    });
    live.push(h);
    await h.setup({
      onInbound: vi.fn(),
      onInboundEvent: vi.fn(),
      onMetadata: vi.fn(),
      onAction: vi.fn(),
    } as unknown as ChannelSetup);
    await until(() => w.calls.some((c) => c.path === "/engines/selected"));
    expect(calls).toEqual([
      {
        role: "executor",
        runtime_providers: ["anthropic", "openai", "local", "custom", "cursor", "copilot"],
        reuse_held: true,
        task_id: "t1",
        attempt: 1,
      },
    ]);
    expect(w.onecli.agents[0]!.secrets).toHaveLength(1); // no Codex secret in the vault
    expect(selectEngine).toHaveBeenCalledWith("claude");
    expect(
      w.calls.some((c) => c.path === "/accounts/release" && c.search === "?role=executor"),
    ).toBe(true);
  });

  it("names the preferred executor of the leased account in the task", () => {
    const text = renderTask(task() as any, new Map(), "claude");
    expect(text).toContain("Preferred EXECUTOR for this task: claude");
    expect(renderTask(task() as any, new Map())).not.toContain("Preferred EXECUTOR");
  });

  // Leases are bound to a claimed task, so the account is re-evaluated at the next
  // claim once `rebalanceMs` has passed, never while idle or mid-task.
  it("adopts a recovered higher-priority account at the next claim, without touching active work", async () => {
    // Only openai was usable for the first task; by the next claim Argus ranks anthropic first.
    const w = world({
      leases: [codexLease(), lease("claude-1", "sk-ant")],
      tasks: [task(), { ...task(), id: "t2" }],
    });
    const recycleRuntime = vi.fn(async () => undefined);
    const selectEngine = vi.fn(async (_engine: string) => true);
    const h = harness(w, true, { rebalanceMs: 30, recycleRuntime, selectEngine });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    expect(recycleRuntime).not.toHaveBeenCalled();
    await new Promise((r) => setTimeout(r, 60));
    await h.adapter.deliver("local", h.thread(), { kind: "chat", content: "COMPLETE: done" });
    await until(() => selectEngine.mock.calls.some((c) => c[0] === "claude"), 5000);
    expect(selectEngine.mock.calls.map((c) => c[0])).toEqual(["codex", "claude"]);
    expect(recycleRuntime).toHaveBeenCalledTimes(1);
    const selected = w.calls.filter((c) => c.path === "/engines/selected").map((c) => c.body);
    expect(selected.map((b) => b.provider)).toEqual(["openai", "anthropic"]);
  });

  it("reports runtime_switch_failed when the rebalance cannot stop the old runtime", async () => {
    const w = world({
      leases: [codexLease(), lease("claude-1", "sk-ant")],
      tasks: [task(), { ...task(), id: "t2" }],
    });
    const recycleRuntime = vi.fn(async () => {
      throw new Error("session s1 did not stop");
    });
    const h = harness(w, true, { rebalanceMs: 30, recycleRuntime });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await new Promise((r) => setTimeout(r, 60));
    await h.adapter.deliver("local", h.thread(), { kind: "chat", content: "COMPLETE: done" });
    await until(() => w.calls.some((c) => c.path === "/tasks/t2/fail"), 5000);
    const fail = w.calls.find((c) => c.path === "/tasks/t2/fail")!;
    expect(fail.body.reason).toBe("runtime_switch_failed");
    expect(fail.body.error).toContain("could not be stopped");
  });

  it("stops the stuck runtime before it claims again after runtime_switch_failed", async () => {
    const w = world({
      leases: [codexLease(), lease("claude-1", "sk-ant"), lease("claude-1", "sk-ant")],
      tasks: [task(), { ...task(), id: "t2" }, { ...task(), id: "t3" }],
    });
    const claimsAtRecycle: number[] = [];
    const recycleRuntime = vi.fn(async () => {
      claimsAtRecycle.push(w.calls.filter((c) => c.path === "/tasks/claim").length);
      if (recycleRuntime.mock.calls.length === 1) throw new Error("session s1 did not stop");
    });
    const h = harness(w, true, { rebalanceMs: 30, recycleRuntime });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await new Promise((r) => setTimeout(r, 60));
    await h.adapter.deliver("local", h.thread(), { kind: "chat", content: "COMPLETE: done" });
    await until(() => h.inbound.length === 2, 5000); // t3 runs once the runtime stopped
    const fail = w.calls.find((c) => c.path === "/tasks/t2/fail")!;
    expect(fail.body.reason).toBe("runtime_switch_failed");
    // The failed stop (during t2's lease) and the retry both happen before t3 is claimed.
    expect(recycleRuntime).toHaveBeenCalledTimes(2);
    expect(claimsAtRecycle).toEqual([2, 2]);
  });

  it("on lease_account_unusable drops the account and stops the runtime before the next claim", async () => {
    const w = world({
      leases: [codexLease(), lease("claude-1", "sk-ant")],
      tasks: [task(), { ...task(), id: "t2" }],
    });
    let claims = 0;
    const inner = w.fetchImpl;
    const fetchImpl = (async (url: string, init: RequestInit) => {
      if (String(url).endsWith("/tasks/claim") && ++claims === 2) {
        w.calls.push({ who: "argus", method: "POST", path: "/tasks/claim", body: {}, search: "" });
        return new Response(
          JSON.stringify({ detail: { code: "lease_account_unusable", message: "exhausted" } }),
          { status: 409 },
        );
      }
      return inner(url, init);
    }) as unknown as typeof fetch;
    const claimsAtRecycle: number[] = [];
    const recycleRuntime = vi.fn(async () => {
      claimsAtRecycle.push(w.calls.filter((c) => c.path === "/tasks/claim").length);
    });
    const h = harness({ ...w, fetchImpl }, true, { recycleRuntime });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", h.thread(), { kind: "chat", content: "COMPLETE: done" });
    await until(() => h.inbound.length === 2, 5000); // t2 runs on the next account
    expect(w.calls.some((c) => c.path === "/accounts/release" && c.search === "?role=engine")).toBe(
      true,
    );
    // Stopped once, after the refused claim and before t2 was claimed.
    expect(claimsAtRecycle).toEqual([2]);
    const selected = w.calls.filter((c) => c.path === "/engines/selected").map((c) => c.body);
    expect(selected.map((b) => b.account_id)).toEqual(["a-codex-1", "a-claude-1"]);
  });

  it("keeps the established account when a re-evaluation finds nothing better or nothing usable", async () => {
    const w = world({
      leases: [codexLease(), "no_account", "no_account"],
      tasks: [task(), { ...task(), id: "t2" }],
    });
    const selectEngine = vi.fn(async (_engine: string) => true);
    const h = harness(w, true, { rebalanceMs: 20, selectEngine });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await new Promise((r) => setTimeout(r, 40));
    await h.adapter.deliver("local", h.thread(), { kind: "chat", content: "COMPLETE: done" });
    await until(() => h.inbound.length === 2, 5000);
    expect(w.calls.filter((c) => c.path === "/accounts/lease").length).toBeGreaterThanOrEqual(2);
    expect(selectEngine).toHaveBeenCalledTimes(1);
    expect(w.calls.some((c) => /\/exhausted$/.test(c.path))).toBe(false);
    expect(w.calls.some((c) => c.path === "/tasks/t2/fail")).toBe(false);
  });

  it("leases afresh at once when the held account does not fit the next task's pin", async () => {
    const strata = lease("strata", "", {
      provider: "local",
      credential: { base_url: "http://host.docker.internal:8080" },
    });
    const w = world({
      leases: [codexLease(), strata],
      tasks: [task(), { ...task(), id: "t2", harness_account: "a-strata" }],
    });
    const recycleRuntime = vi.fn(async () => undefined);
    // An hour between re-evaluations: only the pin makes the second claim lease.
    const h = harness(w, true, { rebalanceMs: 3_600_000, recycleRuntime });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", h.thread(), { kind: "chat", content: "COMPLETE: done" });
    await until(() => h.inbound.length === 2, 5000);
    const engineLeases = w.calls.filter(
      (c) => c.path === "/accounts/lease" && (c.body?.role ?? "engine") === "engine",
    );
    expect(engineLeases).toHaveLength(2);
    expect(recycleRuntime).toHaveBeenCalledTimes(1); // the Codex runtime stops first
    const selected = w.calls.filter((c) => c.path === "/engines/selected").map((c) => c.body);
    expect(selected.map((b) => b.account_id)).toEqual(["a-codex-1", "a-strata"]);
  });

  it("never rotates while a task is in flight", async () => {
    const w = world({
      leases: [codexLease(), "no_account", lease("claude-1", "sk-ant")],
      tasks: [task()],
    });
    const selectEngine = vi.fn(async (_engine: string) => true);
    const h = harness(w, true, { rebalanceMs: 5, selectEngine });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    await new Promise((r) => setTimeout(r, 100)); // the task is never finished
    expect(selectEngine).toHaveBeenCalledTimes(1);
    expect(w.calls.filter((c) => c.path === "/accounts/lease")).toHaveLength(2);
  });
});

describe("custom (OpenRouter) engine accounts", () => {
  const target = { identifier: "g", name: "G" };
  const settings = {
    base_url: "https://openrouter.ai/api/v1",
    label: "openrouter",
    model: "deepseek/deepseek-v4.1-flash-1017",
    provider: null,
  };

  it("lists custom among engine and executor runtime providers", () => {
    // a custom account also serves a delegated CLI (`executor_settings`, the delegate's
    // CUSTOM_KEY_ENV), so the executor role lists it too
    expect(runtimeProviders("engine")).toContain("custom");
    expect(runtimeProviders("executor")).toContain("custom");
  });

  it("applyLeases does not write a OneCLI secret for a custom engine lease", async () => {
    const w = world({ leases: [] });
    const onecli = new OneCliClient("http://127.0.0.1:10254", w.fetchImpl);
    const engine = await applyLeasesDetailed(onecli, target, [
      lease("or", "sk-or-key", {
        provider: "custom",
        credential: { api_key: "sk-or-key" },
        settings,
      }),
    ]);
    expect(engine.engine).toBe("claude");
    expect(w.onecli.secrets).toHaveLength(0);
  });

  it("applyLeases still applies a separate cursor executor lease", async () => {
    const w = world({ leases: [] });
    await applyLeasesDetailed(new OneCliClient("http://127.0.0.1:10254", w.fetchImpl), target, [
      lease("or", "sk-or-key", {
        provider: "custom",
        credential: { api_key: "sk-or-key" },
        settings,
      }),
      cursorLease(),
    ]);
    expect(w.onecli.secrets.some((s) => s.name.includes("-cursor"))).toBe(true);
    expect(w.onecli.secrets.some((s) => s.name.includes("-custom"))).toBe(false);
  });
});

// -- quota failover (account-selection.spec.md 3.6) -----------------------------------

function quotaNoticesFixturePath(): string {
  // install.sh copies the canonical fixture beside this test in NanoClaw src/channels/.
  const installed = fileURLToPath(new URL("./argus-quota-notices.json", import.meta.url));
  if (existsSync(installed)) return installed;
  return fileURLToPath(
    new URL("../../packages/core/tests/fixtures/quota-notices.json", import.meta.url),
  );
}

describe("quota notices shared with Argus", () => {
  const cases = JSON.parse(readFileSync(quotaNoticesFixturePath(), "utf8")) as Array<{
    provider: string;
    notice: string;
    now?: string;
    exhausted: boolean;
    until?: string | null;
    reset_status?: string;
  }>;
  it.each(cases)("$provider: $notice", (c) => {
    const f = classifyFailure(
      c.notice,
      c.provider === "anthropic" ? "anthropic" : c.provider === "custom" ? "custom" : "openai",
      c.now ? new Date(c.now) : undefined,
    );
    expect(f.kind).toBe(c.exhausted ? "exhausted" : "failed");
    if (c.exhausted) {
      // The fixture's `until` is any ISO form with a zone: compare instants.
      const got = (f as { until?: string }).until;
      expect(got ? new Date(got).getTime() : null).toBe(
        c.until ? new Date(c.until).getTime() : null,
      );
    }
    if (c.exhausted && c.reset_status) {
      expect((f as { resetStatus?: string }).resetStatus).toBe(c.reset_status);
    }
  });
});

describe("a Claude rate limit that only the structured field or the log shows", () => {
  const GENERIC = "The agent run failed. Check the logs for details.";
  const resets = "2026-10-20T23:50:00.000Z";
  const A = lease("a", "sk-ant-AAAA");
  const B = lease("b", "sk-ant-BBBB");

  it("classifyProviderFailure reads claude/rate_limit for an anthropic account only", () => {
    const v = { provider: "claude", kind: "rate_limit", resets_at: resets };
    expect(classifyProviderFailure(v, "anthropic")).toEqual({
      kind: "exhausted",
      reason: `Rate limit reached (resets ${resets})`,
      until: resets,
      code: "quota_exhausted",
      source: "structured",
      resetStatus: "stated",
    });
    expect(
      classifyProviderFailure({ provider: "claude", kind: "rate_limit" }, "anthropic"),
    ).toEqual({
      kind: "exhausted",
      reason: "Rate limit reached",
      code: "quota_exhausted",
      source: "structured",
      resetStatus: "absent",
    });
    expect(
      classifyProviderFailure(
        { provider: "claude", kind: "rate_limit", resets_at: "nope" },
        "anthropic",
      ),
    ).toMatchObject({ kind: "exhausted", reason: "Rate limit reached", resetStatus: "absent" });
    expect(classifyProviderFailure(null, "anthropic")).toBeNull();
    expect(classifyProviderFailure(v, "openai")).toBeNull();
    expect(classifyProviderFailure(v, undefined)).toBeNull();
    expect(classifyProviderFailure({ provider: "claude", kind: "other" }, "anthropic")).toBeNull();
  });

  it("classifyLogTail takes the last Rate limit line and ignores other providers", () => {
    const tail = `x\nRate limit [five_hour] (resets 2026-10-20T20:00:00Z)\nmore\nRate limit [five_hour] (resets ${resets})\n`;
    expect(classifyLogTail(tail, "anthropic")).toMatchObject({ kind: "exhausted", until: resets });
    expect(classifyLogTail(tail, "openai")).toBeNull();
    expect(classifyLogTail("nothing here", "anthropic")).toBeNull();
  });

  it("classifyLogTail reads Claude Code's session-limit line, as logged or in a transcript", () => {
    const now = new Date("2026-10-20T22:06:00Z");
    const line = "You've hit your session limit · resets 2am (UTC)";
    const transcript = `{"type":"assistant","error":"rate_limit","message":{"model":"<synthetic>","content":[{"type":"text","text":"${line}"}]}}\n`;
    for (const tail of [`x\n${line}\n`, transcript]) {
      expect(classifyLogTail(tail, "anthropic", now)).toMatchObject({
        kind: "exhausted",
        code: "quota_exhausted",
        source: "log",
        reason: "Rate limit reached (resets 2026-10-21T02:00:00.000Z)",
        until: "2026-10-21T02:00:00.000Z",
        resetStatus: "stated",
      });
    }
    expect(
      classifyLogTail("You've hit your weekly limit · resets soon", "anthropic", now),
    ).toMatchObject({
      kind: "exhausted",
      code: "quota_exhausted",
      source: "log",
      reason: "Rate limit reached",
      resetStatus: "absent",
    });
    expect(classifyLogTail(line, "openai", now)).toBeNull();
  });

  it("generic notice + structured rate_limit: exhausts, switches, resends on the same attempt", async () => {
    const w = world({ leases: [A, B], tasks: [task()] });
    const h = harness(w);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", h.thread(), {
      kind: "chat",
      content: {
        text: GENERIC,
        failureNotice: true,
        providerFailure: { provider: "claude", kind: "rate_limit", resets_at: resets },
      },
    });
    const report = w.calls.find((c) => c.path === "/accounts/a-a/exhausted")!;
    expect(report.body).toMatchObject({ until: resets, task_id: "t1", attempt: 1 });
    await until(() => h.inbound.length === 2);
    expect(h.inbound[1].content.text).toContain("account was switched");
    expect(w.calls.some((c) => c.path.endsWith("/fail"))).toBe(false);
    expect(w.onecli.secrets[0]!.value).toBe("sk-ant-BBBB");
  });

  it("generic notice + log tail: same flow, reset parsed from the log", async () => {
    const w = world({ leases: [A, B], tasks: [task()] });
    const h = harness(
      w,
      true,
      {},
      {
        agentLogTail: async () => `Rate limit [five_hour] (resets ${resets})\n`,
      },
    );
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", h.thread(), failure(GENERIC));
    const report = w.calls.find((c) => c.path === "/accounts/a-a/exhausted")!;
    expect(report.body).toMatchObject({ until: resets, attempt: 1 });
    await until(() => h.inbound.length === 2);
    expect(w.calls.some((c) => c.path.endsWith("/fail"))).toBe(false);
  });

  describe("Claude Code's session-limit line (incident of 2026-10-20)", () => {
    const LINE = "You've hit your session limit · resets 2am (UTC)";
    const NEXT_2AM = "2026-10-21T02:00:00.000Z";
    beforeEach(() => {
      vi.useFakeTimers({ toFake: ["Date"] });
      vi.setSystemTime(new Date("2026-10-20T22:06:00Z"));
    });
    afterEach(() => vi.useRealTimers());

    it.each([
      ["the line as the notice", failure(LINE)],
      [
        "the generic notice + the runner's providerFailure",
        {
          kind: "chat",
          content: {
            text: GENERIC,
            failureNotice: true,
            providerFailure: { provider: "claude", kind: "rate_limit", resets_at: NEXT_2AM },
          },
        },
      ],
    ])(
      "%s: A exhausted until the reset, the task resent on B in the same attempt",
      async (_n, msg) => {
        const w = world({ leases: [A, B], tasks: [task()] });
        const h = harness(w);
        live.push(h.adapter);
        await h.adapter.setup(h.setup);
        await until(() => h.inbound.length === 1);
        await h.adapter.deliver("local", h.thread(), msg as any);
        await until(() => h.inbound.length === 2);
        const report = w.calls.find((c) => c.path === "/accounts/a-a/exhausted")!;
        expect(report.body).toMatchObject({ until: NEXT_2AM, task_id: "t1", attempt: 1 });
        expect(h.inbound[1].content.text).toContain("account was switched");
        expect(w.calls.some((c) => c.path.endsWith("/fail"))).toBe(false);
        expect(w.onecli.secrets[0]!.value).toBe("sk-ant-BBBB");
      },
    );

    it.each([
      `I think ${LINE}`,
      "The session limit resets 2am (UTC), so I paused",
      "Connection reset by peer (ECONNRESET)",
    ])("model text or a generic error (%s) stays a plain failure", async (text) => {
      const w = world({ leases: [A, B], tasks: [task()] });
      const h = harness(w);
      live.push(h.adapter);
      await h.adapter.setup(h.setup);
      await until(() => h.inbound.length === 1);
      await h.adapter.deliver("local", h.thread(), failure(text));
      await until(() => w.calls.some((c) => c.path === "/tasks/t1/fail"));
      expect(w.calls.some((c) => c.path.endsWith("/exhausted"))).toBe(false);
    });
  });

  it("a generic notice with nothing else is still a plain failure", async () => {
    const w = world({ leases: [A, B], tasks: [task()] });
    const h = harness(w, true, {}, { agentLogTail: async () => "all fine" });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", h.thread(), failure(GENERIC));
    await until(() => w.calls.some((c) => c.path === "/tasks/t1/fail"));
    expect(w.calls.some((c) => c.path.endsWith("/exhausted"))).toBe(false);
  });

  it("a generic notice carries the last lines of the agent log as its cause", async () => {
    const w = world({ leases: [A, B], tasks: [task()] });
    const log = "starting\nfatal: no checkout in /workspace\nexit 1\n";
    const h = harness(w, true, {}, { agentLogTail: async () => log });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", h.thread(), failure(GENERIC));
    await until(() => w.calls.some((c) => c.path === "/tasks/t1/fail"));
    const body = w.calls.find((c) => c.path === "/tasks/t1/fail")!.body as { error: string };
    expect(body.error).toContain("fatal: no checkout in /workspace");
    expect(w.calls.some((c) => c.path.endsWith("/exhausted"))).toBe(false);
  });
});

describe("one session per attempt", () => {
  it("closes the old shared session, runs the attempt on its thread, then closes its session", async () => {
    const w = world({ leases: [lease("a", "sk-ant-AAAA")], tasks: [task()] });
    const closeSession = vi.fn(async (_thread: string | null) => undefined);
    const h = harness(w, true, {}, { closeSession });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    expect(closeSession.mock.calls).toEqual([[null]]);
    expect(h.thread()).toBe("argus:t1:1");
    await h.adapter.deliver("local", h.thread(), { kind: "chat", content: "COMPLETE: done" });
    await until(() => closeSession.mock.calls.length === 2);
    expect(closeSession.mock.calls[1]).toEqual(["argus:t1:1"]);
  });

  it("stops the runtime before the next claim when the session did not close", async () => {
    const w = world({
      leases: [lease("a", "sk-ant-AAAA"), lease("a", "sk-ant-AAAA")],
      tasks: [task(), { ...task(), id: "t2" }],
    });
    const claimsAtRecycle: number[] = [];
    const recycleRuntime = vi.fn(async () => {
      claimsAtRecycle.push(w.calls.filter((c) => c.path === "/tasks/claim").length);
    });
    const closeSession = vi.fn(async (thread: string | null) => {
      if (thread === "argus:t1:1") throw new Error("session s1 did not stop");
    });
    const h = harness(w, true, { recycleRuntime }, { closeSession });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", h.thread(), { kind: "chat", content: "COMPLETE: done" });
    await until(() => h.inbound.length === 2);
    expect(h.thread()).toBe("argus:t2:1");
    expect(claimsAtRecycle[0]).toBe(1); // stopped before t2 was claimed
  });
});

describe("closeSessionFromEnv (real hook, NanoClaw modules faked)", () => {
  afterEach(() => {
    nanoclaw.running.clear();
    nanoclaw.killed.length = 0;
    nanoclaw.exits.clear();
    nanoclaw.sessions.length = 0;
  });

  it("closes the thread's session, then stops its container", async () => {
    nanoclaw.sessions.push(
      { id: "s-old", thread_id: null, status: "active" },
      { id: "s1", thread_id: "argus:t1:1", status: "active" },
    );
    nanoclaw.running.add("s1");
    const closing = closeSessionFromEnv("argus:t1:1");
    await until(() => nanoclaw.exits.has("s1"));
    expect(nanoclaw.sessions.find((s) => s.id === "s1")!.status).toBe("closed");
    nanoclaw.exits.get("s1")!();
    await closing;
    expect(nanoclaw.killed).toEqual(["s1"]);
    expect(nanoclaw.sessions.find((s) => s.id === "s-old")!.status).toBe("active");
  });

  it("closes the top-level session of the old shared wiring", async () => {
    nanoclaw.sessions.push({ id: "s-old", thread_id: null, status: "active" });
    await closeSessionFromEnv(null);
    expect(nanoclaw.sessions[0].status).toBe("closed");
    expect(nanoclaw.killed).toEqual([]); // nothing was running
  });

  it("does nothing for a thread that never got a session", async () => {
    await closeSessionFromEnv("argus:t9:1");
    expect(nanoclaw.killed).toEqual([]);
  });
});

describe("no usable account at claim time", () => {
  it("tells Argus so its ready tasks show as awaiting quota", async () => {
    const w = world({ leases: ["no_account"], tasks: [task()] });
    const h = harness(w);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    // the lease retry sleeps 5 s before the task is given back
    await until(() => w.calls.some((c) => c.path === "/accounts/awaiting-quota"), 7000);
    const call = w.calls.find((c) => c.path === "/accounts/awaiting-quota")!;
    expect(call.body).toMatchObject({
      runtime_providers: ["anthropic", "openai", "local", "custom"],
    });
    expect(w.calls.some((c) => c.path === "/tasks/claim")).toBe(true);
    expect(w.calls.find((c) => c.path === "/tasks/t1/fail")!.body).toMatchObject({
      reason: "quota",
    });
  }, 10_000);
});

describe("Codex usage limit and expired login (account-selection.spec.md 3.6)", () => {
  const NOW = new Date("2026-10-20T12:00:00Z");

  it("a structured codex rate_limit exhausts an openai account, with the reset's provenance", () => {
    const stated = classifyProviderFailure(
      {
        provider: "codex",
        kind: "rate_limit",
        notice: "You've hit your usage limit (resets 2026-10-21T03:00:00Z)",
      },
      "openai",
      NOW,
    );
    expect(stated).toMatchObject({
      kind: "exhausted",
      code: "quota_exhausted",
      source: "structured",
      resetStatus: "stated",
      until: "2026-10-21T03:00:00.000Z",
    });
    const words = classifyProviderFailure(
      { provider: "codex", kind: "rate_limit", reset_text: "Nov 19th, 2026 2:08 PM" },
      "openai",
      NOW,
    );
    expect(words).toMatchObject({
      resetStatus: "ambiguous",
      resetText: "Nov 19th, 2026 2:08 PM",
    });
    expect((words as { until?: string }).until).toBeUndefined();
    const bare = classifyProviderFailure({ provider: "codex", kind: "rate_limit" }, "openai", NOW);
    expect(bare).toMatchObject({ resetStatus: "absent", reason: "Codex usage limit reached" });
    expect(
      classifyProviderFailure({ provider: "codex", kind: "rate_limit" }, "anthropic"),
    ).toBeNull();
  });

  it("an expired login is auth_expired", () => {
    expect(
      classifyProviderFailure({ provider: "codex", kind: "auth_expired" }, "openai"),
    ).toMatchObject({
      kind: "exhausted",
      code: "auth_expired",
    });
    expect(
      classifyProviderFailure({ provider: "claude", kind: "auth_expired" }, "anthropic"),
    ).toMatchObject({
      kind: "auth_rejected",
      reason: "Claude authentication failed",
      source: "structured",
    });
    expect(classifyFailure("Error: 401 token_expired", "openai")).toMatchObject({
      kind: "exhausted",
      code: "auth_expired",
    });
  });

  it("a structured JSON error counts, a model merely talking about limits does not", () => {
    const json = JSON.stringify({
      error: { codexErrorInfo: "UsageLimitExceeded", message: "usage limit" },
    });
    expect(classifyFailure(json, "openai")).toMatchObject({
      kind: "exhausted",
      code: "quota_exhausted",
    });
    expect(classifyFailure("I explained that the usage limit resets daily", "openai").kind).toBe(
      "failed",
    );
  });

  it("a time without a zone is never accepted", () => {
    const f = classifyFailure("Usage limit reached (resets 2026-10-21T03:00:00)", "anthropic", NOW);
    expect(f).toMatchObject({ kind: "exhausted", resetStatus: "ambiguous" });
    expect((f as { until?: string }).until).toBeUndefined();
  });

  it("sanitizeNotice drops tokens, query strings and control characters", () => {
    const out = sanitizeNotice(
      "bad\nline Bearer abcdefghij1234 sk-abcdefghijkl https://x.test/a?token=s3cret&b=1 eyJhbGciOiJIUzI1NiJ9abc",
    );
    expect(out).not.toMatch(/abcdefghij1234|sk-abc|s3cret|eyJhbGci|\n/);
    expect(out).toContain("https://x.test/a");
  });

  it("classifyLogTail reads a codex limit sentence for an openai account only", () => {
    const tail = "x\n[agent] Error: You've hit your usage limit. try again at 3:05 PM\n";
    expect(classifyLogTail(tail, "openai", NOW)).toMatchObject({
      kind: "exhausted",
      source: "log",
    });
    expect(classifyLogTail(tail, "anthropic", NOW)).toBeNull();
    const envelope = '{"error":{"message":"x","codexErrorInfo":"usageLimitExceeded"}}';
    expect(classifyLogTail(`a\n${envelope}\n`, "openai", NOW)).toMatchObject({
      kind: "exhausted",
      source: "log",
    });
  });

  it("classifyLogTail ignores quota words in a prompt or tool output", () => {
    const tail = [
      "user: please handle insufficient_quota and usage_limit_reached in the client",
      "You've hit your usage limit. try again at 3:05 PM",
      "tool output: insufficient_quota",
      '{"message":"insufficient_quota","note":"fixture"}',
      "> Error handling: insufficient_quota is returned by the API",
    ].join("\n");
    expect(classifyLogTail(tail, "openai", NOW)).toBeNull();
  });
});

// -- usage relay: the host posts what the container reports -------------------------------

describe("usage relay in the channel", () => {
  const usageRecord = (
    over: Record<string, unknown> = {},
    payload: Record<string, unknown> = {},
  ) => ({
    v: 1,
    kind: "usage",
    source: "delegate",
    role: "executor",
    task_id: "t1",
    attempt: 1,
    account_id: "a-claude-1",
    payload: {
      task_id: "t1",
      model: "claude-sonnet-5-5",
      engine: "claude",
      measurement: "measured",
      input_tokens: 10,
      output_tokens: 5,
      cached_input_tokens: 2,
      cost_usd: null,
      run_key: "r1:t1:ag:codex:claude:s",
      ...payload,
    },
    ...over,
  });
  const write = (file: string, ...records: unknown[]) =>
    appendFileSync(file, records.map((r) => `${JSON.stringify(r)}\n`).join(""));

  async function run(leases: ReturnType<typeof world>["leases"], reply = "COMPLETE: done") {
    const dir = mkdtempSync(path.join(os.tmpdir(), "argus-usage-"));
    const file = path.join(dir, "argus-task-log.jsonl");
    const w = world({ leases, tasks: [task()] });
    const h = harness(w, true, {}, { delegateLogFile: async () => file, logPollMs: 20 });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    const usageFile = path.join(dir, "argus-usage.jsonl");
    return {
      w,
      dir,
      usageFile,
      finish: () => h.adapter.deliver("local", h.thread(), { kind: "chat", content: reply }),
      posts: () => w.calls.filter((c) => /^\/accounts\/[^/]+\/usage$/.test(c.path)),
    };
  }

  it("posts engine and executor usage to the accounts this host holds, before completing", async () => {
    const r = await run([codexLease(), lease("claude-1", "sk-ant", { role: "executor" })]);
    write(
      r.usageFile,
      usageRecord(),
      // the harness (engine role) on the codex account
      usageRecord(
        { source: "harness", role: "engine", account_id: "a-codex-1" },
        { engine: "codex", run_key: "h1:t1:1:th:1", model: "gpt-x" },
      ),
      // wrong account, other task, forged role: none of these may be posted
      usageRecord({ account_id: "a-someone-else" }, { run_key: "bad-account" }),
      usageRecord({ task_id: "t9", attempt: 1 }, { task_id: "t9", run_key: "other-task" }),
      usageRecord({ role: "admin" }, { run_key: "bad-role" }),
    );
    await r.finish();
    const posts = r.posts();
    expect(posts.map((c) => [c.path, c.body.run_key, c.body.role]).sort()).toEqual([
      ["/accounts/a-claude-1/usage", "r1:t1:ag:codex:claude:s", "executor"],
      ["/accounts/a-codex-1/usage", "h1:t1:1:th:1", "engine"],
    ]);
    const order = r.w.calls.map((c) => c.path);
    expect(order.lastIndexOf("/accounts/a-claude-1/usage")).toBeLessThan(
      order.indexOf("/tasks/t1/complete"),
    );
    expect(JSON.stringify(posts)).not.toContain("account_id");
    rmSync(r.dir, { recursive: true });
  });

  it("an executor on the engine's own account posts once against it and takes no second lease", async () => {
    const r = await run([lease("claude-1", "sk-ant")]);
    write(r.usageFile, usageRecord({ account_id: "a-claude-1" }));
    await r.finish();
    expect(r.posts().map((c) => c.path)).toEqual(["/accounts/a-claude-1/usage"]);
    expect(
      r.w.calls.filter((c) => c.path === "/accounts/lease" && c.body?.role === "executor").length,
    ).toBeLessThanOrEqual(1);
    rmSync(r.dir, { recursive: true });
  });
});

// -- executor lease on a harness that only orchestrates (custom/local harness specs) ------

describe("Cursor first on a local or custom engine lease", () => {
  const settings = {
    base_url: "https://openrouter.ai/api/v1",
    label: "openrouter",
    model: "deepseek/deepseek-v4.1-flash-1017",
    provider: null,
  };
  const custom = () =>
    lease("or", "sk-or-key", {
      provider: "custom",
      credential: { api_key: "sk-or-key" },
      settings,
    });

  it("executorFirst names cursor only for local and custom engines", () => {
    expect(executorFirst("custom")).toBe("cursor");
    expect(executorFirst("local")).toBe("cursor");
    expect(executorFirst("anthropic")).toBeNull();
    expect(executorFirst("openai")).toBeNull();
    expect(executorFirst(null)).toBeNull();
  });

  it("follows the leased executor on a custom engine lease, not Cursor", () => {
    const anthropic = lease("claude-1", "sk-ant", { role: "executor" });
    expect(preferredExecutorForLease(custom(), anthropic)).toBe("claude");
    expect(preferredExecutorForLease(custom(), null)).toBeNull();
  });

  async function executorCalls(answers: Array<"no_account" | AccountLease>) {
    const w = world({ leases: [custom()], tasks: [task()] });
    const calls: any[] = [];
    const inner = w.fetchImpl;
    const fetchImpl = (async (url: string, init: RequestInit) => {
      const body = init.body ? JSON.parse(init.body as string) : undefined;
      if (String(url).endsWith("/accounts/lease") && body?.role === "executor") {
        calls.push(body);
        const next = answers.shift();
        if (!next || next === "no_account")
          return new Response(
            JSON.stringify({ detail: { code: "no_account", message: "no usable account" } }),
            { status: 409 },
          );
        return new Response(JSON.stringify(next), { status: 200 });
      }
      return inner(url, init);
    }) as unknown as typeof fetch;
    const h = createArgusAdapter({
      url: "http://argus.test",
      bootstrapToken: "boot",
      capabilities: [],
      name: "w",
      fetch: fetchImpl,
      claimWaitS: 1,
      accounts: {
        models: true,
        onecli: new OneCliClient("http://127.0.0.1:10254", fetchImpl),
        resolveTarget: async () => ({ identifier: "ag-group-1", name: "Worker" }),
        selectEngine: vi.fn(async (_engine: string) => true),
      },
    });
    live.push(h);
    await h.setup({
      onInbound: vi.fn(),
      onInboundEvent: vi.fn(),
      onMetadata: vi.fn(),
      onAction: vi.fn(),
    } as unknown as ChannelSetup);
    await until(() => w.calls.some((c) => c.path === "/engines/selected"));
    return { calls: calls.map((c) => c.runtime_providers), w };
  }

  it("leases the Cursor executor first so its stub is mounted", async () => {
    const cursor = lease("cur", "", {
      provider: "cursor",
      role: "executor",
      credential: { api_key: "crsr" },
    });
    const { calls } = await executorCalls([cursor]);
    expect(calls).toEqual([["cursor"]]);
  });

  it("falls back to any executor provider when no Cursor account is usable", async () => {
    const { calls } = await executorCalls(["no_account", "no_account"]);
    expect(calls).toEqual([
      ["cursor"],
      ["anthropic", "openai", "local", "custom", "cursor", "copilot"],
    ]);
  });

  it("skips the executor lease when difficulty_executors is empty", async () => {
    const w = world({
      leases: [custom()],
      tasks: [{ ...task(), difficulty_executors: [] }],
    });
    const h = createArgusAdapter({
      url: "http://argus.test",
      bootstrapToken: "boot",
      capabilities: [],
      name: "w",
      fetch: w.fetchImpl,
      claimWaitS: 1,
      accounts: {
        models: true,
        onecli: new OneCliClient("http://127.0.0.1:10254", w.fetchImpl),
        resolveTarget: async () => ({ identifier: "ag-group-1", name: "Worker" }),
        selectEngine: vi.fn(async (_engine: string) => true),
      },
    });
    live.push(h);
    await h.setup({
      onInbound: vi.fn(),
      onInboundEvent: vi.fn(),
      onMetadata: vi.fn(),
      onAction: vi.fn(),
    } as unknown as ChannelSetup);
    await until(() => w.calls.some((c) => c.path === "/engines/selected"));
    expect(
      w.calls.filter((c) => c.path === "/accounts/lease" && c.body?.role === "executor"),
    ).toEqual([]);
    expect(
      w.calls.some((c) => c.path === "/accounts/release" && c.search === "?role=executor"),
    ).toBe(true);
  });
});

// -- quota resume -----------------------------------------------------------------------

describe("quota resume in the channel (quota-resume.spec.md 3.3-3.5)", () => {
  const SESSION = "5b1c0f3e-aaaa-bbbb-cccc-0123456789ab";
  const offer = { session_id: SESSION, engine: "anthropic", node: "ag", from_attempt: 1 };
  const paused = (over: Record<string, unknown> = {}) => ({
    ...task(),
    attempt: 2,
    quota_resume: { ...offer, ...over },
  });
  const NOTE = "[Argus steer] From argus:quota-resume";

  // The session mailboxes of one host, by thread: they outlive a channel restart.
  let boxes: Map<string, string>;
  beforeEach(() => {
    boxes = new Map();
  });
  const continuation = (thread: string) => {
    const file = boxes.get(thread);
    if (!file) return null;
    const db = new Database(file, { readonly: true });
    try {
      return (
        (
          db.prepare("SELECT value FROM session_state WHERE key = 'continuation:claude'").get() as
            | { value: string }
            | undefined
        )?.value ?? null
      );
    } finally {
      db.close();
    }
  };
  /** NanoClaw's resume seam with a real seed: a new session's outbound.db per thread. */
  const seeding = () =>
    vi.fn(async (sessionId: string, thread: string) => {
      if (!boxes.has(thread)) {
        const dir = mkdtempSync(path.join(os.tmpdir(), "argus-qr-session-"));
        groupDirs.push(dir);
        const db = new Database(path.join(dir, "outbound.db"));
        db.exec(OUTBOUND_SCHEMA);
        db.close();
        boxes.set(thread, path.join(dir, "outbound.db"));
      }
      return seedClaudeContinuation(boxes.get(thread)!, sessionId);
    });

  /** One claim of `claimed` with a scripted take (`first`, or an HTTP status). */
  async function claim(
    claimed: ReturnType<typeof paused>,
    take: boolean | number,
    resume?: (id: string, thread: string) => Promise<boolean>,
    readable = true,
  ) {
    const w = world({ leases: [lease("a", "sk-ant-AAAA")], tasks: [claimed] });
    const takes: any[] = [];
    const fetchImpl = (async (url: string, init: RequestInit) => {
      if (new URL(url).pathname.endsWith("/quota-resume")) {
        takes.push(JSON.parse(init.body as string));
        if (typeof take === "number") return new Response("down", { status: take });
        return new Response(JSON.stringify({ first: take }), { status: 200 });
      }
      return w.fetchImpl(url, init);
    }) as unknown as typeof fetch;
    const h = harness(
      { ...w, fetchImpl },
      true,
      {},
      {
        nativeSessions: {
          observe: async () => null,
          readable: async (id) => readable && id === SESSION,
          resume,
        },
      },
    );
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.teardown();
    live.splice(live.indexOf(h.adapter), 1);
    return { takes, text: h.inbound[0].content.text as string, thread: h.thread() };
  }

  it("resumes the same session: seeds the attempt's thread and sends the note once", async () => {
    const resume = seeding();
    const run = await claim(paused(), true, resume);
    expect(resume).toHaveBeenCalledWith(SESSION, "argus:t1:2");
    expect(run.thread).toBe("argus:t1:2");
    expect(continuation("argus:t1:2")).toBe(SESSION);
    expect(run.takes).toEqual([{ attempt: 2, outcome: "resumed" }]);
    expect(run.text.startsWith(NOTE)).toBe(true);
    expect(run.text.split(NOTE)).toHaveLength(2);
  });

  it("after a crash, the reclaimed attempt resumes again without a second note", async () => {
    const resume = seeding();
    await claim(paused(), true, resume);
    // A new channel process claims the same attempt: the seed is already there.
    const again = await claim(paused(), false, resume);
    expect(again.takes).toEqual([{ attempt: 2, outcome: "resumed" }]);
    expect(continuation("argus:t1:2")).toBe(SESSION);
    expect(again.text).not.toContain(NOTE);
  });

  it("a stale lease or a failed take resumes without the note", async () => {
    for (const status of [409, 503]) {
      boxes = new Map();
      const run = await claim(paused(), status, seeding());
      expect(run.takes).toHaveLength(1);
      expect(run.text).not.toContain(NOTE);
    }
  });

  it("falls back to a fresh session, telling Argus why, and never seeds", async () => {
    const cases: Array<[string, ReturnType<typeof paused>, boolean, boolean, string]> = [
      ["other node", paused({ node: "elsewhere" }), true, true, "other_node"],
      ["other engine", paused({ engine: "openai" }), true, true, "other_engine"],
      ["transcript gone", paused(), true, false, "unreadable"],
      ["host cannot resume", paused(), false, true, "not_available"],
    ];
    for (const [name, claimed, canResume, readable, reason] of cases) {
      boxes = new Map();
      const resume = seeding();
      const run = await claim(claimed, true, canResume ? resume : undefined, readable);
      expect(run.takes, name).toEqual([{ attempt: 2, outcome: "fallback", reason }]);
      expect(resume, name).not.toHaveBeenCalled();
      expect(run.text, name).not.toContain(NOTE);
    }
  });

  it("reports resume_failed when the seed does not take, and leaks nothing", async () => {
    // Another continuation already holds the thread's session.
    const resume = seeding();
    await resume("9d0e1f2a-aaaa-bbbb-cccc-0123456789ab", "argus:t1:2");
    const run = await claim(paused(), true, resume);
    expect(run.takes).toEqual([{ attempt: 2, outcome: "fallback", reason: "resume_failed" }]);
    expect(continuation("argus:t1:2")).toBe("9d0e1f2a-aaaa-bbbb-cccc-0123456789ab");
    const thrown = await claim(paused(), true, async () => {
      throw new Error("LEAK-7731 /data/v2-sessions/group-1/.claude-shared/x.jsonl");
    });
    expect(thrown.takes).toEqual([{ attempt: 2, outcome: "fallback", reason: "resume_failed" }]);
    expect(thrown.text).not.toContain(NOTE);
    expect(thrown.text).not.toContain("LEAK-7731");
  });
});
