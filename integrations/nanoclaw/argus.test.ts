import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChannelSetup } from "./adapter.js";
import {
  type ArgusConfig,
  createArgusAdapter,
  fromEnv,
  parseReply,
  renderTask,
  taskAuthorizesMerge,
} from "./argus.js";

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

  it("denies merge by default on repository tasks and documents status reads", () => {
    const repoTask = task({
      repository_url: "https://git.example.test/org/repo.git",
    });
    expect(taskAuthorizesMerge(repoTask as any)).toBe(false);
    const text = renderTask(repoTask as any, new Map());
    expect(text).toContain("status = commit statuses for the PR head SHA");
    expect(text).toContain("Do not merge pull requests unless");
    expect(text).not.toContain("force_merge");
    expect(text).not.toContain("This task explicitly authorizes merge");
    expect(text).not.toMatch(/\bRadar\b/i);
  });

  it("adds merge preconditions when the task explicitly authorizes delivery merge", () => {
    const repoTask = task({
      repository_url: "https://git.example.test/org/repo.git",
      input: { delivery: { allow_merge: true } },
    });
    expect(taskAuthorizesMerge(repoTask as any)).toBe(true);
    const text = renderTask(repoTask as any, new Map());
    expect(text).toContain("This task explicitly authorizes merge");
    expect(text).toContain("exact SHA");
    expect(text).toContain("P1–P3");
    expect(text).toContain("force_merge");
    expect(text).toContain("GiteaApi.request");
    expect(text).toContain("manual Production deploys");
    expect(text).not.toContain("Do not merge pull requests unless");
  });

  it("does not treat spec mentions, negations, or non-boolean input as merge grants", () => {
    const repo = { repository_url: "https://git.example.test/org/repo.git" };
    const deny = "Do not merge pull requests unless";
    const grant = "This task explicitly authorizes merge";
    for (const over of [
      { spec: "See docs: allow_merge: true is only valid in task input." },
      { spec: "Do not set allow_merge: true without owner approval." },
      { input: { delivery: { allow_merge: "true" } } },
    ]) {
      const repoTask = task({ ...repo, ...over });
      expect(taskAuthorizesMerge(repoTask as any)).toBe(false);
      const text = renderTask(repoTask as any, new Map());
      expect(text).toContain(deny);
      expect(text).not.toContain(grant);
    }
  });

  it("requires argus-delegate for implementation and forbids direct CLIs", () => {
    const text = renderTask(task() as any, new Map());
    expect(text).toContain("/usr/local/bin/argus-delegate EXECUTOR");
    expect(text).toContain("EXECUTOR agent");
    expect(text).toContain("the NanoClaw engine remains Codex");
    expect(text).not.toContain("When the engine is Cursor");
    expect(text).toContain(
      "/usr/local/bin/argus-delegate agent \\\n    --cwd <task-checkout> --prompt-file <prompt.txt>",
    );
    expect(text).toContain("Example (normal implementation, not --read-only):");
    expect(text).not.toMatch(/argus-delegate agent[^\n]*--read-only/);
    expect(text).toContain(
      "Do not invoke agent, cursor-agent, claude, codex, or opencode directly",
    );
    expect(text).toContain("prepareExecutorEnv");
    expect(text).toContain("argus-model-policy.json");
    expect(text).toContain("delegate JSON");
    expect(text).toContain("resume this same task");
    expect(text).toContain('"ASK:"');
    expect(text).toContain('"FAIL:"');
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

// -- results of any length and real error messages -----------------------------------

import { createHash } from "node:crypto";
import {
  artifactName,
  describeError,
  headWithin,
  SUMMARY_MAX_CHARS,
  withHashSuffix,
} from "./argus.js";

/** Like fakeArgus, with an artifact store and scriptable failures on /complete. */
function resultArgus(
  opts: { completeFailures?: Array<Response | "network">; uploadFailures?: Response[] } = {},
) {
  const calls: Call[] = [];
  const artifacts: Array<{
    id: string;
    name: string;
    attempt: number;
    sha256: string;
    bytes: Uint8Array;
  }> = [];
  const failures = [...(opts.completeFailures ?? [])];
  const uploadFailures = [...(opts.uploadFailures ?? [])];
  const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });
  const fetchImpl = (async (url: string, init: RequestInit) => {
    const u = new URL(url);
    const path = u.pathname.replace(/^\/api/, "");
    const isBytes = path.endsWith("/artifacts") && init.method === "POST";
    const body = !isBytes && init.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ method: init.method ?? "GET", path: path + u.search, body });
    if (path === "/agents/register") {
      return json(201, { token: "t", agent: { id: "agent-1" }, heartbeat_interval_s: 1 });
    }
    if (path === "/agents/me/heartbeat")
      return json(200, { state: "active", renewed: [], rejected: [] });
    if (path === "/agents/me/leave") return json(200, {});
    if (path === "/tasks/claim") {
      return new Promise<Response>((_, reject) =>
        init.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        ),
      );
    }
    if (isBytes) {
      if (uploadFailures.length) return uploadFailures.shift()!;
      const name = u.searchParams.get("name") as string;
      const bytes = new Uint8Array(init.body as ArrayBuffer);
      const attempt = Number(u.searchParams.get("attempt"));
      // The server's rule (artifacts/service.py): unique per (task, attempt, name),
      // answered with 409 `artifact_exists`.
      if (artifacts.some((a) => a.name === name && a.attempt === attempt)) {
        return json(409, {
          detail: {
            code: "artifact_exists",
            message: `artifact '${name}' already exists for attempt ${attempt}`,
          },
        });
      }
      const a = {
        id: `art-${artifacts.length + 1}`,
        name,
        attempt,
        sha256: createHash("sha256").update(bytes).digest("hex"),
        bytes,
      };
      artifacts.push(a);
      return json(201, { id: a.id, name, sha256: a.sha256, size: bytes.length });
    }
    if (path.endsWith("/artifacts") && init.method === "GET") return json(200, artifacts);
    if (path.endsWith("/complete") && failures.length) {
      const f = failures.shift()!;
      if (f === "network") throw new TypeError("fetch failed");
      return f;
    }
    return json(200, {});
  }) as unknown as typeof fetch;
  return { calls, fetchImpl, artifacts };
}

async function inFlight(fake: ReturnType<typeof resultArgus>, over = {}) {
  const tasks = [task(over)];
  const base = fake.fetchImpl;
  const wrapped = (async (url: string, init: RequestInit) => {
    const path = new URL(url).pathname.replace(/^\/api/, "");
    if (path === "/tasks/claim" && tasks.length) {
      fake.calls.push({ method: "POST", path, body: undefined });
      return new Response(JSON.stringify(tasks.shift()), { status: 200 });
    }
    return base(url, init);
  }) as unknown as typeof fetch;
  const h = harness({ calls: fake.calls, fetchImpl: wrapped } as ReturnType<typeof fakeArgus>);
  live.push(h.adapter as any);
  await h.adapter.setup(h.setup);
  await until(() => h.inbound.length === 1);
  return h;
}

const reply = (h: Awaited<ReturnType<typeof inFlight>>, text: string) =>
  h.adapter.deliver("local", null, { kind: "chat", content: { text } });
const completes = (fake: ReturnType<typeof resultArgus>) =>
  fake.calls.filter((c) => c.path.endsWith("/complete"));

describe("results of any length", () => {
  it("a short COMPLETE is sent as the summary, with no artifact (legacy behaviour)", async () => {
    const fake = resultArgus();
    const h = await inFlight(fake);
    await reply(h, "COMPLETE: argus-onecli-smoke-ok");
    expect(completes(fake)).toHaveLength(1);
    expect(completes(fake)[0]?.body.result).toEqual({ summary: "argus-onecli-smoke-ok" });
    expect(fake.artifacts).toHaveLength(0);
  });

  it("a response of exactly the limit stays a plain summary", async () => {
    const fake = resultArgus();
    const h = await inFlight(fake);
    await reply(h, `COMPLETE: ${"a".repeat(SUMMARY_MAX_CHARS)}`);
    expect(completes(fake)[0]?.body.result.summary).toHaveLength(SUMMARY_MAX_CHARS);
    expect(fake.artifacts).toHaveLength(0);
  });

  it("an oversized multibyte response is attached whole and summarized within the limit", async () => {
    const fake = resultArgus();
    const h = await inFlight(fake);
    const body = `# Título\n${"Especificação funcional ação é ü 日本語 😀 ".repeat(900)}\nFIM`;
    await reply(h, `COMPLETE: ${body}`);
    const [a] = fake.artifacts;
    expect(fake.artifacts).toHaveLength(1);
    expect(new TextDecoder().decode(a?.bytes)).toBe(body); // the whole text, byte for byte
    expect(a?.name).toBe("result-attempt-1.md");
    const result = completes(fake)[0]?.body.result;
    expect(Array.from(result.summary).length).toBeLessThanOrEqual(SUMMARY_MAX_CHARS);
    expect(result.artifacts).toEqual([a?.id]);
    expect(result.summary.startsWith("# Título")).toBe(true);
    expect(result.summary).toContain(a?.id);
    expect(result.summary).toContain(createHash("sha256").update(body).digest("hex").slice(0, 16));
    // The first call to the API after the upload was the complete, with the lease's attempt.
    expect(completes(fake)[0]?.body.attempt).toBe(1);
  });

  it("never splits a surrogate pair at the cut", () => {
    const head = headWithin("😀".repeat(50), 7);
    expect(Array.from(head)).toHaveLength(7);
    expect(head).toBe("😀".repeat(7));
  });

  it("a transient failure on complete is retried without a second artifact", async () => {
    const fake = resultArgus({
      completeFailures: ["network", new Response("<html>bad gateway</html>", { status: 502 })],
    });
    const h = await inFlight(fake);
    const text = `COMPLETE: ${"x".repeat(SUMMARY_MAX_CHARS + 500)}`;
    await expect(reply(h, text)).rejects.toThrow(); // the host's delivery retry path gets it
    await expect(reply(h, text)).rejects.toThrow(/Bad Gateway|<html>|502/i);
    await reply(h, text); // third attempt succeeds
    expect(fake.artifacts).toHaveLength(1); // reused, not duplicated
    expect(completes(fake)).toHaveLength(3);
    expect(completes(fake)[2]?.body.result.artifacts).toEqual(["art-1"]);
    expect(fake.calls.some((c) => c.path.endsWith("/fail"))).toBe(false);
  });

  it("a different body under the same artifact name gets its own name", async () => {
    const fake = resultArgus();
    const h = await inFlight(fake);
    fake.artifacts.push({
      id: "art-old",
      name: "result-attempt-1.md",
      attempt: 1,
      sha256: "0".repeat(64),
      bytes: new Uint8Array(),
    });
    await reply(h, `COMPLETE: ${"y".repeat(SUMMARY_MAX_CHARS + 1)}`);
    expect(fake.artifacts).toHaveLength(2);
    expect(fake.artifacts[1]?.name).toMatch(/^result-attempt-1-[0-9a-f]{8}\.md$/);
  });

  it("a permanent rejection fails the attempt with the real reason, not claimed forever", async () => {
    const detail = [
      {
        loc: ["body", "result", "summary"],
        msg: "String should have at most 10000 characters",
        type: "string_too_long",
      },
    ];
    const fake = resultArgus({
      completeFailures: [new Response(JSON.stringify({ detail }), { status: 422 })],
    });
    const h = await inFlight(fake);
    await reply(h, "COMPLETE: short but the server says no");
    const fail = fake.calls.find((c) => c.path.endsWith("/fail"));
    expect(fail?.body.error).toMatch(
      /422 validation_failed.*result\.summary: String should have at most 10000/,
    );
    expect(fail?.body.error).not.toMatch(/\[object Object\]/);
    // The task is released from this worker: a later reply finds nothing in flight.
    await reply(h, "COMPLETE: stray");
    expect(completes(fake)).toHaveLength(1);
  });
});

const withFile = (
  h: Awaited<ReturnType<typeof inFlight>>,
  text: string,
  filename: string,
  data: string,
) =>
  h.adapter.deliver("local", null, {
    kind: "chat",
    content: { text },
    files: [{ filename, data: Buffer.from(data, "utf8") }],
  });

describe("files the agent attaches", () => {
  it("are uploaded as artifacts and listed in the completion, whatever message carried them", async () => {
    const fake = resultArgus();
    const h = await inFlight(fake);
    const doc = "# Especificação\nconteúdo 日本語 😀\n";
    await withFile(h, "Segue o documento em anexo.", "Relatório final (v2).md", doc); // a progress note
    expect(completes(fake)).toHaveLength(0);
    expect(fake.artifacts).toHaveLength(1);
    expect(fake.artifacts[0]?.name).toBe("Relatorio-final-v2-.md");
    expect(new TextDecoder().decode(fake.artifacts[0]?.bytes)).toBe(doc);
    await reply(h, "COMPLETE: entregue");
    expect(completes(fake)[0]?.body.result).toEqual({ summary: "entregue", artifacts: ["art-1"] });
  });

  it("a file-only message (no text) is stored, not dropped", async () => {
    const fake = resultArgus();
    const h = await inFlight(fake);
    await h.adapter.deliver("local", null, {
      kind: "chat",
      content: {},
      files: [{ filename: "a.txt", data: Buffer.from("hi") }],
    });
    expect(fake.artifacts.map((a) => a.name)).toEqual(["a.txt"]);
  });

  it("a transient upload failure is retried and never duplicates the artifact", async () => {
    const fake = resultArgus({ uploadFailures: [new Response("oops", { status: 503 })] });
    const h = await inFlight(fake);
    await expect(withFile(h, "anexo", "n.md", "x")).rejects.toThrow();
    await withFile(h, "anexo", "n.md", "x");
    await withFile(h, "anexo", "n.md", "x"); // delivered again: reused (same sha)
    expect(fake.artifacts).toHaveLength(1);
  });

  it("an attachment the server refuses permanently is named in the summary; the task still completes", async () => {
    const fake = resultArgus({
      uploadFailures: [
        new Response(
          JSON.stringify({ detail: { code: "too_large", message: "artifact exceeds the limit" } }),
          { status: 413 },
        ),
      ],
    });
    const h = await inFlight(fake);
    await withFile(h, "anexo", "big.bin", "x");
    await reply(h, "COMPLETE: pronto");
    const result = completes(fake)[0]?.body.result;
    expect(result.summary).toMatch(
      /^pronto\n\n\[Not stored: big\.bin \(413 too_large: artifact exceeds the limit\)\]$/,
    );
    expect(result.artifacts).toBeUndefined();
  });

  it("an oversized response plus an attachment lists both artifacts", async () => {
    const fake = resultArgus();
    const h = await inFlight(fake);
    await withFile(h, "anexo", "extra.json", "{}");
    await reply(h, `COMPLETE: ${"z".repeat(SUMMARY_MAX_CHARS + 10)}`);
    expect(completes(fake)[0]?.body.result.artifacts.sort()).toEqual(["art-1", "art-2"]);
    expect(fake.artifacts.map((a) => a.name)).toEqual(["extra.json", "result-attempt-1.md"]);
  });

  it("sanitizes names to what Argus accepts", () => {
    expect(artifactName("../../etc/.passwd")).toBe("passwd");
    expect(artifactName("___")).toBe("attachment");
    expect(artifactName("a b.md")).toBe("a-b.md");
    expect(artifactName(`${"n".repeat(300)}.md`)).toHaveLength(200);
  });
});

const SHA_A = createHash("sha256").update("a").digest("hex");
const fullSha = (data: string) => createHash("sha256").update(data).digest("hex");
const sha8 = (data: string) => fullSha(data).slice(0, 8);
const seed = (
  fake: ReturnType<typeof resultArgus>,
  name: string,
  data: string,
  attempt = 1,
  id = `seed-${fake.artifacts.length + 1}`,
) =>
  fake.artifacts.push({
    id,
    name,
    attempt,
    sha256: createHash("sha256").update(data).digest("hex"),
    bytes: new TextEncoder().encode(data),
  });
const SERVER_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$/; // artifacts/service.py _NAME

describe("withHashSuffix", () => {
  it("goes before the extension, or at the end without one", () => {
    expect(withHashSuffix("report.md", SHA_A)).toBe(`report-${SHA_A.slice(0, 8)}.md`);
    expect(withHashSuffix("notes.txt", SHA_A)).toBe(`notes-${SHA_A.slice(0, 8)}.txt`);
    expect(withHashSuffix("data.json", SHA_A)).toBe(`data-${SHA_A.slice(0, 8)}.json`);
    expect(withHashSuffix("Makefile", SHA_A)).toBe(`Makefile-${SHA_A.slice(0, 8)}`);
    expect(withHashSuffix("a.tar.gz", SHA_A)).toBe(`a.tar-${SHA_A.slice(0, 8)}.gz`);
  });

  it("stays a valid name of at most 200 characters, keeping the extension", () => {
    for (const name of [`${"n".repeat(200)}`, `${"n".repeat(196)}.md`, `${"n".repeat(190)}.json`]) {
      const out = withHashSuffix(name, SHA_A);
      expect(out.length).toBeLessThanOrEqual(200);
      expect(SERVER_NAME.test(out)).toBe(true);
      expect(
        out.endsWith(
          `-${SHA_A.slice(0, 8)}${name.includes(".") ? name.slice(name.lastIndexOf(".")) : ""}`,
        ),
      ).toBe(true);
    }
    expect(withHashSuffix(artifactName(`${"x".repeat(300)}.txt`), SHA_A)).toHaveLength(200);
  });
});

describe("artifact name collisions", () => {
  // One conflicting body per extension kind; each pair: what the server already holds vs the new bytes.
  const kinds: Array<[string, string]> = [
    ["notes.md", "# a"],
    ["notes.txt", "texto"],
    ["data.json", "{}"],
    ["README", "sem extensão"],
  ];

  for (const [name, body] of kinds) {
    it(`${name}: different content under a taken name is stored under a hashed name, byte for byte`, async () => {
      const fake = resultArgus();
      const h = await inFlight(fake);
      seed(fake, name, "old body", 1, "art-old");
      const fresh = `${body} ação 日本語 😀`;
      await withFile(h, "anexo", name, fresh);
      const added = fake.artifacts.filter((a) => a.id !== "art-old");
      expect(added).toHaveLength(1);
      expect(added[0]?.name).toBe(withHashSuffix(name, fullSha(fresh)));
      expect(added[0]?.name).not.toBe(name);
      expect(new TextDecoder().decode(added[0]?.bytes)).toBe(fresh);
      expect(fake.artifacts.find((a) => a.id === "art-old")?.name).toBe(name); // the old one is untouched
    });

    it(`${name}: delivering the same content again reuses the artifact (no duplicate)`, async () => {
      const fake = resultArgus();
      const h = await inFlight(fake);
      await withFile(h, "anexo", name, body);
      await withFile(h, "anexo", name, body);
      expect(fake.artifacts).toHaveLength(1);
    });
  }

  it("after a restart (nothing remembered) identical content found on the server is reused", async () => {
    const fake = resultArgus();
    seed(fake, "notes.txt", "same", 1, "art-have");
    const h = await inFlight(fake);
    await withFile(h, "anexo", "notes.txt", "same");
    expect(fake.artifacts).toHaveLength(1);
    await reply(h, "COMPLETE: ok");
    expect(completes(fake)[0]?.body.result.artifacts).toEqual(["art-have"]);
  });

  it("a long name is cut in the stem, never the extension, and stays valid on the server", async () => {
    const fake = resultArgus();
    const h = await inFlight(fake);
    const long = `${"l".repeat(300)}.json`;
    const base = artifactName(long);
    expect(base).toHaveLength(200);
    seed(fake, base, "old");
    await withFile(h, "anexo", long, "new");
    const added = fake.artifacts[1];
    expect(added?.name).toHaveLength(200);
    expect(SERVER_NAME.test(added?.name ?? "")).toBe(true);
    expect(added?.name).toBe(withHashSuffix(base, fullSha("new")));
    expect(added?.name.endsWith(`-${sha8("new")}.json`)).toBe(true);
  });

  it("only the current attempt counts: a same-named artifact of an earlier attempt is not reused", async () => {
    const fake = resultArgus();
    seed(fake, "notes.txt", "same", 0, "art-attempt-0"); // the identical bytes, but another attempt
    const h = await inFlight(fake); // attempt 1
    await withFile(h, "anexo", "notes.txt", "same");
    expect(fake.artifacts).toHaveLength(2);
    const mine = fake.artifacts[1];
    expect(mine?.attempt).toBe(1);
    expect(mine?.name).toBe("notes.txt"); // free under attempt 1: no hashed name needed
    await reply(h, "COMPLETE: ok");
    expect(completes(fake)[0]?.body.result.artifacts).toEqual([mine?.id]);
  });

  it("the hashed-name fallback does not pick up an earlier attempt's artifact either", async () => {
    const fake = resultArgus();
    const h = await inFlight(fake); // attempt 1
    const hashed = withHashSuffix("notes.txt", fullSha("new"));
    seed(fake, "notes.txt", "old", 1, "art-conflict"); // forces the fallback
    seed(fake, hashed, "new", 0, "art-attempt-0"); // same name and bytes, earlier attempt
    seed(fake, hashed, "new", 1, "art-attempt-1"); // the one that must be reused
    await withFile(h, "anexo", "notes.txt", "new");
    expect(fake.artifacts).toHaveLength(3); // nothing new was stored
    await reply(h, "COMPLETE: ok");
    expect(completes(fake)[0]?.body.result.artifacts).toEqual(["art-attempt-1"]);
  });

  it("a hashed name taken by different content fails permanently with a clear reason", async () => {
    const fake = resultArgus();
    const h = await inFlight(fake);
    const hashed = withHashSuffix("notes.txt", fullSha("new"));
    seed(fake, "notes.txt", "old", 1, "art-1");
    seed(fake, hashed, "something else", 1, "art-2");
    await withFile(h, "anexo", "notes.txt", "new"); // recorded, not thrown: the task can still complete
    await reply(h, "COMPLETE: ok");
    expect(completes(fake)[0]?.body.result.summary).toMatch(
      /Not stored: notes\.txt \(422 artifact_name_taken/,
    );
  });

  it("the oversized result uses the same helper (result-attempt-1.md conflict -> hashed .md)", async () => {
    const fake = resultArgus();
    const h = await inFlight(fake);
    seed(fake, "result-attempt-1.md", "other", 1, "art-old");
    const text = "w".repeat(SUMMARY_MAX_CHARS + 5);
    await reply(h, `COMPLETE: ${text}`);
    const added = fake.artifacts.find((a) => a.id !== "art-old");
    expect(added?.name).toBe(`result-attempt-1-${sha8(text)}.md`);
    expect(new TextDecoder().decode(added?.bytes)).toBe(text);
  });
});

describe("describeError", () => {
  it("renders domain errors, validation lists, strings and bodies without JSON", () => {
    expect(
      describeError({ detail: { code: "stale_attempt", message: "superseded" } }, "Conflict"),
    ).toEqual({
      code: "stale_attempt",
      message: "superseded",
    });
    expect(
      describeError(
        {
          detail: [
            { loc: ["body", "a"], msg: "bad" },
            { loc: ["body", "b", "c"], msg: "worse" },
          ],
        },
        "x",
      ),
    ).toEqual({ code: "validation_failed", message: "a: bad; b.c: worse" });
    expect(describeError({ detail: "plain" }, "x")).toEqual({ code: "error", message: "plain" });
    expect(describeError(null, "Bad Gateway", "<html>oops</html>").message).toBe(
      "<html>oops</html>",
    );
    expect(describeError({ detail: { weird: true } }, "x").message).toBe('{"weird":true}');
  });
});
