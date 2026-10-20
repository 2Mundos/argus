import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChannelSetup } from "./adapter.js";
import { createArgusAdapter } from "./argus.js";
import {
  type AccountLease,
  applyLease,
  applyLeases,
  applyLeasesDetailed,
  applyRepositoryLease,
  CURSOR_EXPIRY_MARGIN_MS,
  classifyFailure,
  classifyProviderFailure,
  exchangeCursorApiKey,
  jwtExpiryMs,
  OneCliClient,
  releaseRepositoryLease,
} from "./argus-accounts.js";

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
    const onecli = new OneCliClient("http://onecli.test", w.fetchImpl);
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
    await releaseRepositoryLease(onecli, target, []);
    expect(w.onecli.agents[0]!.secrets).toEqual([]);
  });
});

/** One fetch for both services, recording calls and holding OneCLI state. */
function world(opts: {
  leases: Array<AccountLease | "no_account">;
  tasks?: any[];
  exhaustedResponse?: (n: number) => any;
  heartbeatIntervalS?: number;
  rejectHeartbeat?: boolean;
}) {
  const calls: Call[] = [];
  const leases = [...opts.leases];
  const tasks = [...(opts.tasks ?? [])];
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
    if (u.host === "onecli.test") {
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
        rejected: opts.rejectHeartbeat ? [{ task_id: "t1" }] : [],
      });
    if (path === "/agents/me/leave") return json(200, {});
    if (path === "/accounts/lease") {
      const next = leases.shift();
      if (next === undefined || next === "no_account") {
        return json(409, { detail: { code: "no_account", message: "no usable account" } });
      }
      return json(200, next);
    }
    if (/^\/accounts\/[^/]+\/exhausted$/.test(path)) {
      const n = ++exhaustedCount;
      if (opts.exhaustedResponse) return json(200, opts.exhaustedResponse(n));
      const next = leases.shift();
      if (next && next !== "no_account") return json(200, { lease: next, task_blocked: false });
      return json(200, { lease: null, earliest_reset: null, task_blocked: true });
    }
    if (path === "/tasks/claim") {
      const t = tasks.shift();
      if (t) return json(200, t);
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

function harness(
  w: ReturnType<typeof world>,
  withAccounts = true,
  extra: Partial<NonNullable<Parameters<typeof createArgusAdapter>[0]["accounts"]>> = {},
) {
  const inbound: any[] = [];
  const setup = {
    onInbound: vi.fn(async (_p: string, _t: string | null, m: any) => void inbound.push(m)),
    onInboundEvent: vi.fn(),
    onMetadata: vi.fn(),
    onAction: vi.fn(),
  } as unknown as ChannelSetup;
  const adapter = createArgusAdapter({
    url: "http://argus.test",
    bootstrapToken: "boot",
    capabilities: [],
    name: "w",
    fetch: w.fetchImpl,
    claimWaitS: 1,
    accounts: withAccounts
      ? {
          providers: ["anthropic"],
          onecli: new OneCliClient("http://onecli.test", w.fetchImpl),
          resolveTarget: async () => ({ identifier: "ag-group-1", name: "Worker" }),
          ...extra,
        }
      : undefined,
  });
  return { adapter, setup, inbound };
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
      new OneCliClient("http://onecli.test", w.fetchImpl),
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
    expect(classifyFailure("Rate limit [five_hour] (resets 2026-10-15T01:00:00.000Z)")).toEqual({
      kind: "exhausted",
      reason: "Rate limit [five_hour] (resets 2026-10-15T01:00:00.000Z)",
      until: "2026-10-15T01:00:00.000Z",
    });
    expect(classifyFailure("Out of credits")).toMatchObject({
      kind: "exhausted",
      until: undefined,
    });
  });
  it("treats everything else as a plain failure, anchored to the start", () => {
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
    const onecli = new OneCliClient("http://onecli.test", w.fetchImpl);
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
      new OneCliClient("http://onecli.test", w.fetchImpl),
      { identifier: "g", name: "n" },
      lease("sub", "", { kind: "subscription", credential: { oauth_token: "sk-ant-oat01-XYZ" } }),
    );
    expect(w.onecli.secrets[0]!.value).toBe("sk-ant-oat01-XYZ");
  });

  it("rejects unknown providers and credentials with no usable field, without echoing values", async () => {
    const w = world({ leases: [] });
    const onecli = new OneCliClient("http://onecli.test", w.fetchImpl);
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
  it("leases and applies an account before it claims anything", async () => {
    const w = world({ leases: [lease("a", "sk-ant-AAAA")], tasks: [task()] });
    const h = harness(w);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    const order = w.calls.map((c) => `${c.who}:${c.path}`);
    expect(order.indexOf("argus:/accounts/lease")).toBeLessThan(
      order.indexOf("argus:/tasks/claim"),
    );
    expect(order.indexOf("onecli:/agents")).toBeLessThan(order.indexOf("argus:/tasks/claim"));
    expect(w.onecli.secrets[0]!.value).toBe("sk-ant-AAAA");
  });

  it("does not claim while no account is usable, and starts once one is", async () => {
    const w = world({ leases: ["no_account", lease("a", "sk-ant-AAAA")], tasks: [task()] });
    const h = harness(w);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await new Promise((r) => setTimeout(r, 200));
    expect(w.calls.some((c) => c.path === "/tasks/claim")).toBe(false);
    await until(() => h.inbound.length === 1, 7000); // the retry sleeps 5 s
  }, 10_000);

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
      null,
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

    await h.adapter.deliver("local", null, { kind: "chat", content: "COMPLETE: done" });
    expect(w.calls.some((c) => c.path === "/tasks/t1/complete")).toBe(true);
  });

  it("with no replacement: Argus blocks the task and the channel lets go of it", async () => {
    const w = world({ leases: [lease("a", "sk-ant-AAAA")], tasks: [task()] });
    const h = harness(w);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", null, failure("Out of credits"));
    expect(w.calls.some((c) => c.path === "/accounts/a-a/exhausted")).toBe(true);
    expect(w.calls.some((c) => c.path.endsWith("/fail"))).toBe(false); // Argus blocked it
    // Anything the agent says now is not for a task we hold.
    const before = w.calls.length;
    await h.adapter.deliver("local", null, { kind: "chat", content: "COMPLETE: late" });
    expect(w.calls.slice(before).some((c) => c.path.endsWith("/complete"))).toBe(false);
  });

  it("gives the account back when it cannot be applied to the vault", async () => {
    const w = world({ leases: [lease("a", "sk-ant-AAAA")], tasks: [task()] });
    const failing = ((url: string, init: RequestInit) =>
      new URL(url).host === "onecli.test"
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
        providers: ["anthropic"],
        onecli: new OneCliClient("http://onecli.test", failing),
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
    expect(w.calls.some((c) => c.path === "/tasks/claim")).toBe(false);
    expect(inbound).toHaveLength(0);
  });

  it("a non-quota failure notice fails the attempt with its text", async () => {
    const w = world({ leases: [lease("a", "sk-ant-AAAA")], tasks: [task()] });
    const h = harness(w);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", null, failure("Failed to authenticate. API Error: 401"));
    const fail = w.calls.find((c) => c.path === "/tasks/t1/fail")!;
    expect(fail.body).toEqual({ attempt: 1, error: "Failed to authenticate. API Error: 401" });
    expect(w.calls.some((c) => c.path.endsWith("/exhausted"))).toBe(false);
  });

  it("without accounts configured: no leasing, no vault calls; failures still fail the attempt", async () => {
    const w = world({ leases: [], tasks: [task()] });
    const h = harness(w, false);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", null, failure("Rate limit"));
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

describe("openai (Codex) credential documents", () => {
  const target = { identifier: "g", name: "G" };

  it("gives OneCLI the whole ChatGPT auth.json as an openai secret for chatgpt.com", async () => {
    const w = world({ leases: [] });
    const engine = await applyLease(
      new OneCliClient("http://onecli.test", w.fetchImpl),
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
      new OneCliClient("http://onecli.test", w.fetchImpl),
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
        new OneCliClient("http://onecli.test", w.fetchImpl),
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
    const onecli = new OneCliClient("http://onecli.test", w.fetchImpl);
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
      providers: ["openai", "anthropic"],
      selectEngine,
    });
    live.push(adapter);
    await adapter.setup(setup);
    await until(() => w.calls.some((c) => c.path === "/engines/selected"));
    const leaseCalls = w.calls.filter((c) => c.path === "/accounts/lease").map((c) => c.body);
    expect(leaseCalls).toEqual([
      { providers: ["openai", "anthropic"] },
      { providers: ["anthropic"], role: "executor" },
    ]);
    expect(selectEngine).toHaveBeenCalledWith("codex");
    const selected = w.calls.find((c) => c.path === "/engines/selected")!.body;
    expect(selected).toEqual({
      engine: "codex",
      provider: "openai",
      account_id: "a-codex-1",
      reason: "lease",
    });
    expect(JSON.stringify(w.calls.filter((c) => c.who === "argus"))).not.toContain("SECRET");
    expect(w.onecli.agents[0]!.secrets).toHaveLength(2);
  });

  it("takes no executor lease when the engine is already the executor's provider", async () => {
    const w = world({ leases: [lease("claude-1", "sk-ant")], tasks: [task()] });
    const selectEngine = vi.fn(async () => false);
    const { adapter, setup } = harness(w, true, {
      providers: ["openai", "anthropic"],
      selectEngine,
    });
    live.push(adapter);
    await adapter.setup(setup);
    await until(() => w.calls.some((c) => c.path === "/engines/selected"));
    expect(w.calls.filter((c) => c.path === "/accounts/lease")).toHaveLength(1);
    expect(selectEngine).toHaveBeenCalledWith("claude");
    expect(w.onecli.agents[0]!.secrets).toHaveLength(1);
  });

  it("still serves Codex when no executor account is usable, and holds no residual secret", async () => {
    const w = world({ leases: [codexLease(), "no_account"], tasks: [task()] });
    const { adapter, setup } = harness(w, true, { providers: ["openai", "anthropic"] });
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
    const { adapter, setup } = harness(w, true, { providers: ["openai", "anthropic"] });
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
    const onecli = new OneCliClient("http://onecli.test", w.fetchImpl);
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
    const onecli = new OneCliClient("http://onecli.test", fetchImpl);
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
    const onecli = new OneCliClient("http://onecli.test", w.fetchImpl);
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
    const onecli = new OneCliClient("http://onecli.test", w.fetchImpl);
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
  it("leases cursor for the executor when executorProviders says so", async () => {
    const w = world({
      leases: [codexLease(), cursorLease()],
      tasks: [task()],
    });
    const { adapter, setup } = harness(w, true, {
      providers: ["openai"],
      executorProviders: ["cursor"],
    });
    live.push(adapter);
    await adapter.setup(setup);
    await until(() => w.calls.some((c) => c.path === "/engines/selected"));
    expect(w.calls.filter((c) => c.path === "/accounts/lease").map((c) => c.body)).toEqual([
      { providers: ["openai"] },
      { providers: ["cursor"], role: "executor" },
    ]);
    expect(w.onecli.secrets.filter((s) => s.name.includes("cursor"))).toHaveLength(2);
  });
});

describe("applyLeasesDetailed", () => {
  it("reports executor JWT expiry for a Cursor executor lease", async () => {
    const w = world({ leases: [] });
    const onecli = new OneCliClient("http://onecli.test", w.fetchImpl);
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
    const h = harness(w, true, { providers: ["openai", "anthropic"] });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    await h.adapter.deliver(
      "local",
      null,
      failure("Rate limit [five_hour] (resets 2026-10-15T01:00:00.000Z)"),
    );
    await until(() => w.calls.some((c) => /\/tasks\/t1\/fail$/.test(c.path)));
    expect(w.calls.some((c) => /\/accounts\/[^/]+\/exhausted$/.test(c.path))).toBe(false);
  });

  it("reports an openai quota error as exhausted for the engine role", async () => {
    const w = world({ leases: [codexLease(), "no_account"], tasks: [task()] });
    const h = harness(w, true, { providers: ["openai", "anthropic"] });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    await h.adapter.deliver("local", null, failure("usage_limit_reached"));
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
    const h = harness(w, true, { providers: ["openai", "anthropic"] });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    await h.adapter.deliver("local", null, msg as any);
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
    expect(call?.body).toEqual({
      reason: "OpenAI/Codex authentication expired",
      role: "engine",
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
    const h = harness(w, true, { providers: ["openai"] });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    await h.adapter.deliver("local", null, structured(ok, false) as any);
    await new Promise((r) => setTimeout(r, 50));
    expect(exhaustedCalls(w)).toEqual([]);
    expect(w.calls.some((c) => /\/tasks\/t1\/fail$/.test(c.path))).toBe(false);
  });

  it("keeps a generic notice without metadata as a plain failure", async () => {
    const { w } = await run([codexLease()], failure("The agent run failed. Check the logs."));
    expect(exhaustedCalls(w)).toEqual([]);
    expect(w.calls.some((c) => /\/tasks\/t1\/fail$/.test(c.path))).toBe(true);
  });

  it("classifyProviderFailure fails closed without an openai account", () => {
    expect(classifyProviderFailure(ok, undefined)).toBeNull();
    expect(classifyProviderFailure(ok, "anthropic")).toBeNull();
    expect(classifyProviderFailure(ok, "openai")).toEqual({
      kind: "exhausted",
      reason: "OpenAI/Codex authentication expired",
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
      const codeTask = { ...task(), required_capabilities: ["repository:write"] };
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
        { providers: ["anthropic"] },
        { providers: ["gitea"], role: "repository" },
      ]);
      await h.adapter.deliver("local", null, {
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
      const completed = w.calls.find((c) => c.path === "/tasks/t1/complete")!;
      expect(completed.body.result.data.delivery.head_sha).toBe("head-1");
      await until(() => w.calls.some((c) => isRepositoryRelease(c)));
      expect(w.calls.filter((c) => isRepositoryRelease(c))).toHaveLength(1);
    },
  );

  it("does not acquire repository credentials for existing non-repository tasks", async () => {
    const w = world({ leases: [lease("engine", "dummy-engine-key")], tasks: [task()] });
    const h = harness(w, true, { repositories: { global: "https://git.example.test/org/repo" } });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    expect(w.calls.filter((c) => c.path === "/accounts/lease")).toHaveLength(1);
    expect(h.inbound[0].content.text).not.toContain("Repository (allowlisted)");
  });

  it.each([
    ["FAIL: cannot continue", "/tasks/t1/fail"],
    ["ASK: need a decision", "/tasks/t1/ask"],
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
      tasks: [{ ...task(), required_capabilities: ["repository:write"] }],
    });
    const h = harness(w, true, { repositories: { global: "https://git.example.test/org/repo" } });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    await h.adapter.deliver("local", null, { kind: "chat", content: { text: reply } });
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
      tasks: [{ ...task(), required_capabilities: ["repository:write"] }],
      heartbeatIntervalS: 1,
      rejectHeartbeat: true,
    });
    const h = harness(w, true, { repositories: { global: "https://git.example.test/org/repo" } });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length > 0);
    await until(() => w.calls.some((call) => isRepositoryRelease(call)));
  });

  it("fails closed before requesting Gitea access without a known base lease set", async () => {
    const w = world({
      leases: [],
      tasks: [{ ...task(), required_capabilities: ["repository:write"] }],
    });
    const h = harness(w, true, {
      providers: [],
      repositories: { global: "https://git.example.test/org/repo" },
    });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => w.calls.some((call) => call.path === "/tasks/t1/fail"));
    expect(w.calls.some((call) => call.path === "/accounts/lease")).toBe(false);
    expect(h.inbound).toHaveLength(0);
  });
});
