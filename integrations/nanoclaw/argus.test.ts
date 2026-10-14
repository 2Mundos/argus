import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChannelSetup } from "./adapter.js";
import { type ArgusConfig, createArgusAdapter, fromEnv, parseReply, renderTask } from "./argus.js";

type Call = { method: string; path: string; body: any };

/** A scripted fake Argus API behind an injected fetch. */
function fakeArgus(
  opts: { tasks?: any[]; completeStatus?: number; heartbeat?: any; claimDraining?: boolean } = {},
) {
  const calls: Call[] = [];
  const tasks = [...(opts.tasks ?? [])];
  const json = (status: number, body: unknown) =>
    new Response(status === 204 ? null : JSON.stringify(body), { status });

  const fetchImpl = (async (url: string, init: RequestInit) => {
    const path = new URL(url).pathname.replace(/^\/api/, "");
    const body = init.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ method: init.method ?? "GET", path, body });
    if (path === "/agents/register") {
      return json(201, {
        token: "argus_agent_x",
        agent: { id: "agent-1" },
        heartbeat_interval_s: 1,
      });
    }
    if (path === "/agents/me/heartbeat") {
      return json(200, opts.heartbeat ?? { state: "active", renewed: [], rejected: [] });
    }
    if (path === "/agents/me/leave") return json(200, { state: "gone" });
    if (path === "/tasks/claim") {
      if (opts.claimDraining) {
        return json(409, { detail: { code: "agent_draining", message: "agent is draining" } });
      }
      const next = tasks.shift();
      if (next) return json(200, next);
      // Long poll: hold until the channel aborts the request.
      return new Promise<Response>((_, reject) => {
        init.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        );
      });
    }
    if (path.startsWith("/knowledge/pages/")) {
      return json(200, { body: "Run `make deploy-stage`, then smoke test." });
    }
    if (path.endsWith("/complete") && opts.completeStatus && opts.completeStatus >= 400) {
      return json(opts.completeStatus, {
        detail: { code: "stale_attempt", message: "superseded" },
      });
    }
    return json(200, {});
  }) as unknown as typeof fetch;
  return { calls, fetchImpl };
}

const task = (over: Record<string, unknown> = {}) => ({
  id: "t1",
  title: "Deploy the app",
  spec: "Ship v1.4 to staging.",
  project: "web",
  attempt: 1,
  acceptance: ["smoke tests pass"],
  knowledge_refs: [
    {
      page_id: "p1",
      title: "Deploy runbook",
      summary: "How to deploy",
      why: "pinned",
      stale: false,
    },
  ],
  ...over,
});

function harness(fake: ReturnType<typeof fakeArgus>, over: Partial<ArgusConfig> = {}) {
  const inbound: any[] = [];
  const setup = {
    onInbound: vi.fn(async (_p: string, _t: string | null, m: any) => {
      inbound.push(m);
    }),
    onInboundEvent: vi.fn(),
    onMetadata: vi.fn(),
    onAction: vi.fn(),
  } as unknown as ChannelSetup;
  let clock = 1_000_000;
  const adapter = createArgusAdapter({
    url: "http://argus.test",
    bootstrapToken: "argus_boot_x",
    capabilities: ["python"],
    name: "worker",
    fetch: fake.fetchImpl,
    now: () => clock,
    claimWaitS: 1,
    ...over,
  });
  return { adapter, setup, inbound, advance: (ms: number) => (clock += ms) };
}

const until = async (cond: () => boolean, ms = 2000) => {
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

describe("parseReply", () => {
  it("recognizes directives case-insensitively and trims", () => {
    expect(parseReply("COMPLETE: all done")).toEqual({ kind: "complete", text: "all done" });
    expect(parseReply("  fail : cannot reach host ")).toEqual({
      kind: "fail",
      text: "cannot reach host",
    });
    expect(parseReply("Ask: Which region?")).toEqual({ kind: "ask", text: "Which region?" });
  });
  it("treats anything else as progress, including look-alikes mid-text", () => {
    expect(parseReply("working on it")).toEqual({ kind: "progress", text: "working on it" });
    expect(parseReply("almost COMPLETE: soon")).toEqual({
      kind: "progress",
      text: "almost COMPLETE: soon",
    });
  });
});

describe("renderTask", () => {
  it("includes identity, spec, acceptance, knowledge and the reply protocol", () => {
    const text = renderTask(task() as any, new Map([["p1", "Step 1\nStep 2"]]));
    expect(text).toContain("[Argus task] Deploy the app");
    expect(text).toContain("Task: t1 · Attempt: 1");
    expect(text).toContain("- smoke tests pass");
    expect(text).toContain("Deploy runbook: How to deploy");
    expect(text).toContain("    Step 2");
    expect(text).toContain('"COMPLETE:"');
  });
});

describe("Argus channel", () => {
  it("registers, claims a task, injects it, and completes on COMPLETE:", async () => {
    const fake = fakeArgus({ tasks: [task()] });
    const h = harness(fake);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);

    await until(() => h.inbound.length === 1);
    expect(h.adapter.isConnected()).toBe(true);
    const reg = fake.calls.find((c) => c.path === "/agents/register")!;
    expect(reg.body).toMatchObject({ bootstrap_token: "argus_boot_x", capabilities: ["python"] });
    const text = h.inbound[0].content.text as string;
    expect(text).toContain("Deploy the app");
    expect(text).toContain("make deploy-stage"); // pinned page body inlined

    await h.adapter.deliver("local", null, {
      kind: "chat",
      content: { text: "COMPLETE: shipped it" },
    });
    const done = fake.calls.find((c) => c.path === "/tasks/t1/complete")!;
    expect(done.body).toEqual({ attempt: 1, result: { summary: "shipped it" } });
    // …and it goes back to claiming.
    await until(() => fake.calls.filter((c) => c.path === "/tasks/claim").length >= 2);
  });

  it("maps FAIL: and ASK: and ends the task for each", async () => {
    for (const [reply, path, body] of [
      ["FAIL: no network", "/tasks/t1/fail", { attempt: 1, error: "no network" }],
      ["ASK: which region?", "/tasks/t1/ask", { attempt: 1, question: "which region?" }],
    ] as const) {
      const fake = fakeArgus({ tasks: [task()] });
      const h = harness(fake);
      live.push(h.adapter);
      await h.adapter.setup(h.setup);
      await until(() => h.inbound.length === 1);
      await h.adapter.deliver("local", null, { kind: "chat", content: reply });
      expect(fake.calls.find((c) => c.path === path)!.body).toEqual(body);
      await h.adapter.teardown();
    }
  });

  it("sends progress at most once per interval and coalesces to the latest", async () => {
    const fake = fakeArgus({ tasks: [task()] });
    const h = harness(fake, { progressIntervalMs: 60 });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);

    const say = (text: string) =>
      h.adapter.deliver("local", null, { kind: "chat", content: { text } });
    await say("step 1");
    await until(() => fake.calls.some((c) => c.path === "/tasks/t1/progress"));
    h.advance(10); // inside the interval
    await say("step 2");
    await say("step 3");
    h.advance(100);
    await until(() => fake.calls.filter((c) => c.path === "/tasks/t1/progress").length === 2, 1000);
    const notes = fake.calls
      .filter((c) => c.path === "/tasks/t1/progress")
      .map((c) => c.body.message);
    expect(notes).toEqual(["step 1", "step 3"]);
  });

  it("drops the reply and moves on when Argus says the task is no longer ours", async () => {
    const fake = fakeArgus({ tasks: [task()], completeStatus: 409 });
    const h = harness(fake);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await expect(
      h.adapter.deliver("local", null, { kind: "chat", content: "COMPLETE: late" }),
    ).resolves.toBeUndefined();
    await until(() => fake.calls.filter((c) => c.path === "/tasks/claim").length >= 2);
  });

  it("ignores replies when no task is in flight", async () => {
    const fake = fakeArgus();
    const h = harness(fake);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await h.adapter.deliver("local", null, { kind: "chat", content: "COMPLETE: nothing" });
    expect(fake.calls.some((c) => c.path.includes("/complete"))).toBe(false);
  });

  it("stops claiming once Argus reports the agent as draining", async () => {
    const fake = fakeArgus({ heartbeat: { state: "draining", renewed: [], rejected: [] } });
    const h = harness(fake);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => fake.calls.some((c) => c.path === "/agents/me/heartbeat"), 2500);
    const claimsBefore = fake.calls.filter((c) => c.path === "/tasks/claim").length;
    await new Promise((r) => setTimeout(r, 1300));
    // A claim already in flight may finish, but no new ones are started.
    expect(fake.calls.filter((c) => c.path === "/tasks/claim").length).toBeLessThanOrEqual(
      claimsBefore + 1,
    );
  });

  it("when drained and idle: leaves Argus, stops claiming, and signals the host", async () => {
    const fake = fakeArgus({ heartbeat: { state: "draining", renewed: [], rejected: [] } });
    const onDrained = vi.fn();
    const h = harness(fake, { onDrained });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => onDrained.mock.calls.length === 1, 3000);
    expect(fake.calls.some((c) => c.path === "/agents/me/leave")).toBe(true);
    expect(h.adapter.isConnected()).toBe(false);
    const claims = fake.calls.filter((c) => c.path === "/tasks/claim").length;
    await new Promise((r) => setTimeout(r, 300));
    expect(fake.calls.filter((c) => c.path === "/tasks/claim").length).toBe(claims);
    expect(onDrained).toHaveBeenCalledTimes(1);
  });

  it("a claim answered agent_draining also completes the drain, without a heartbeat", async () => {
    const fake = fakeArgus({ claimDraining: true });
    const onDrained = vi.fn();
    const h = harness(fake, { onDrained });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => onDrained.mock.calls.length === 1, 500); // well under a heartbeat
    expect(fake.calls.some((c) => c.path === "/agents/me/leave")).toBe(true);
  });

  it("when drained mid-task: finishes the task first, then leaves", async () => {
    const fake = fakeArgus({
      tasks: [task()],
      heartbeat: { state: "draining", renewed: [], rejected: [] },
    });
    const onDrained = vi.fn();
    const h = harness(fake, { onDrained });
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await new Promise((r) => setTimeout(r, 1300)); // a heartbeat reports draining
    expect(onDrained).not.toHaveBeenCalled(); // still working
    expect(fake.calls.some((c) => c.path === "/agents/me/leave")).toBe(false);

    await h.adapter.deliver("local", null, { kind: "chat", content: "COMPLETE: done" });
    await until(() => onDrained.mock.calls.length === 1);
    expect(fake.calls.some((c) => c.path === "/tasks/t1/complete")).toBe(true);
    expect(fake.calls.some((c) => c.path === "/agents/me/leave")).toBe(true);
  });

  it("leaves Argus on teardown", async () => {
    const fake = fakeArgus();
    const h = harness(fake);
    await h.adapter.setup(h.setup);
    await h.adapter.teardown();
    expect(fake.calls.some((c) => c.path === "/agents/me/leave")).toBe(true);
    expect(h.adapter.isConnected()).toBe(false);
  });

  it("factory: disabled without ARGUS_URL or any token, enabled with both", () => {
    const saved = { ...process.env };
    try {
      delete process.env.ARGUS_URL;
      delete process.env.ARGUS_BOOTSTRAP_TOKENS;
      delete process.env.ARGUS_AGENT_TOKEN;
      expect(fromEnv()).toBeNull();
      process.env.ARGUS_URL = "http://argus.test";
      expect(fromEnv()).toBeNull(); // URL but no credentials
      process.env.ARGUS_BOOTSTRAP_TOKENS = "argus_boot_a,argus_boot_b";
      expect(fromEnv()?.channelType).toBe("argus");
    } finally {
      process.env = saved;
    }
  });
});
