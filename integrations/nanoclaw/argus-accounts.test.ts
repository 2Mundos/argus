import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChannelSetup } from "./adapter.js";
import { createArgusAdapter } from "./argus.js";
import {
  type AccountLease,
  applyLease,
  applyLeases,
  classifyFailure,
  OneCliClient,
} from "./argus-accounts.js";

// -- fakes ------------------------------------------------------------------------

type Call = { who: "argus" | "onecli"; method: string; path: string; body: any };

const lease = (name: string, key: string, over: Partial<AccountLease> = {}): AccountLease => ({
  lease_id: `l-${name}`,
  account_id: `a-${name}`,
  name,
  provider: "anthropic",
  kind: "api_key",
  credential: { api_key: key },
  ...over,
});

/** One fetch for both services, recording calls and holding OneCLI state. */
function world(opts: {
  leases: Array<AccountLease | "no_account">;
  tasks?: any[];
  exhaustedResponse?: (n: number) => any;
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
        onecli.secrets.find((s) => s.id === m![1])!.value = body.value;
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
    calls.push({ who: "argus", method, path, body });
    if (path === "/agents/register") {
      return json(201, { token: "t", agent: { id: "ag" }, heartbeat_interval_s: 30 });
    }
    if (path === "/agents/me/heartbeat")
      return json(200, { state: "active", renewed: [], rejected: [] });
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
      (await apply({ oauth_token: "long-lived", claudeAiOauth: { expiresAt: 1 } }))[0]?.value,
    ).toBe("long-lived");
    const err = await apply({
      claudeAiOauth: { accessToken: "t", expiresAt: "2001-01-01T00:00:00Z" },
    }).catch((e) => e);
    expect(String(err.message)).toMatch(/expired/);
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
