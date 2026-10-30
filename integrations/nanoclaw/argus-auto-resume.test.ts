/**
 * The channel's half of the automatic resume rule (failure-classes.spec.md 3.2): the
 * confirmed exit is reported once, retried a bounded number of times on a transient error,
 * never after a conflict, and a plain reply reaches Argus as the attempt's progress before
 * it. Docker is a fake runtime and Argus a scripted fetch; every log text is synthetic.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChannelSetup } from "./adapter.js";
import { type ArgusTask, createArgusAdapter, type RuntimeHooks } from "./argus.js";
import type { SessionEvidence } from "./argus-stderr-tail.js";

const TASK = {
  id: "t1",
  title: "Crashy",
  spec: "Do it.",
  project: "web",
  attempt: 2,
  acceptance: [],
  knowledge_refs: [],
} as unknown as ArgusTask;

// Synthetic secret in the session output; the channel redacts it before any call.
const SECRET = ["sk", "-SYNTHETICAUTORESUME0123456789abcd"].join("");

type Call = { path: string; body: any };

function fakeArgus(failStatuses: number[]) {
  const calls: Call[] = [];
  const queue = [TASK];
  const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });
  const fetchImpl = (async (url: string, init: RequestInit) => {
    const path = new URL(url).pathname.replace(/^\/api/, "");
    const raw = init.body;
    const body = typeof raw === "string" ? JSON.parse(raw) : undefined;
    calls.push({ path, body });
    if (path === "/agents/register")
      return json(201, { token: "t", agent: { id: "ag" }, heartbeat_interval_s: 1 });
    if (path === "/agents/me/heartbeat")
      return json(200, { state: "active", renewed: [], rejected: [] });
    if (path === "/runtime/prepare") return json(200, { state: "disabled", reason: "test" });
    if (path === "/tasks/t1/artifacts") return json(201, { id: "art-1" });
    if (path === "/tasks/t1/fail") {
      const status = failStatuses.shift() ?? 200;
      if (status === 409)
        return json(409, { detail: { code: "not_claimed", message: "task t1 is ready" } });
      if (status >= 400) return new Response("upstream down", { status });
      return json(200, { id: "t1", state: "ready" });
    }
    if (path === "/tasks/claim") {
      const next = queue.shift();
      if (next) return json(200, next);
      return new Promise<Response>((_, reject) =>
        init.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        ),
      );
    }
    return json(200, {});
  }) as unknown as typeof fetch;
  return { calls, fetchImpl };
}

let clock = Date.UTC(2026, 9, 30, 12);
const live: Array<{ teardown: () => Promise<void> }> = [];
afterEach(async () => {
  for (const a of live.splice(0)) await a.teardown();
});

const until = async (cond: () => boolean, ms = 8000) => {
  const start = Date.now();
  while (!cond()) {
    if (Date.now() - start > ms) throw new Error("timed out waiting for condition");
    await new Promise((r) => setTimeout(r, 5));
  }
};

async function start(failStatuses: number[]) {
  const fake = fakeArgus(failStatuses);
  let containers = ["ctr-1"];
  const evidence: SessionEvidence = {
    exitCode: 1,
    logTail: `synthetic line\nfatal: token ${SECRET} rejected`,
    truncated: false,
    complete: true,
  };
  const runtime: RuntimeHooks = {
    host: async () => ({
      host_id: "h",
      run_as: "1:1",
      base_ref: "img",
      base_id: "sha",
      platform: "linux/amd64",
    }),
    apply: async () => undefined,
    usedImage: async () => ({ container: "ctr-1", imageId: "sha" }),
    cleanup: async () => undefined,
    sessions: async () => containers,
    sessionEvidence: async () => evidence,
    sessionActivityAt: async () => null,
    workspaceState: async () => "ok",
  };
  const inbound: unknown[] = [];
  let thread: string | null = null;
  const setup = {
    onInbound: vi.fn(async (_p: string, t: string | null, m: unknown) => {
      thread = t;
      inbound.push(m);
    }),
    onInboundEvent: vi.fn(),
    onMetadata: vi.fn(),
    onAction: vi.fn(),
  } as unknown as ChannelSetup;
  const adapter = createArgusAdapter({
    url: "http://argus.test",
    bootstrapToken: "boot",
    capabilities: ["python"],
    name: "worker",
    fetch: fake.fetchImpl,
    claimWaitS: 1,
    observe: async () => ({ artifacts: [] }),
    progressIntervalMs: 0,
    exitRetryMs: 1,
    now: () => clock,
    runtime,
  });
  live.push(adapter);
  await adapter.setup(setup);
  await until(() => inbound.length === 1);
  const beats = () => fake.calls.filter((c) => c.path === "/agents/me/heartbeat");
  const sessionBeat = async (state: string) => {
    const seen = beats().length;
    await until(() =>
      beats()
        .slice(seen)
        .some((c) => c.body?.leases?.[0]?.observation?.session === state),
    );
  };
  const lose = async () => {
    await sessionBeat("running");
    containers = [];
    await sessionBeat("gone");
    clock += 91_000;
  };
  return { fake, adapter, thread: () => thread, lose };
}

const fails = (f: ReturnType<typeof fakeArgus>) =>
  f.calls.filter((c) => c.path === "/tasks/t1/fail");
const claims = (f: ReturnType<typeof fakeArgus>) =>
  f.calls.filter((c) => c.path === "/tasks/claim");

describe("the confirmed exit reaches Argus (automatic resume, synthetic logs)", () => {
  it("reports session_lost once with the attempt and a redacted, bounded error", async () => {
    const h = await start([]);
    await h.lose();
    await until(() => fails(h.fake).length === 1);
    const [fail] = fails(h.fake);
    expect(fail!.body).toMatchObject({ attempt: 2, reason: "session_lost" });
    const error = fail!.body.error as string;
    expect(error).toMatch(/^session container ctr-1 exited \(code 1\) without a reply/);
    expect(new TextEncoder().encode(error).length).toBeLessThanOrEqual(2048);
    for (const c of h.fake.calls) expect(JSON.stringify(c.body ?? "")).not.toContain("SYNTHETIC");
    // the run is over: the channel claims again and reports nothing more for it
    await until(() => claims(h.fake).length >= 2);
    expect(fails(h.fake)).toHaveLength(1);
  });

  it("retries a transient error with the same report", async () => {
    const h = await start([503]);
    await h.lose();
    await until(() => fails(h.fake).length === 2);
    const [first, second] = fails(h.fake);
    expect(second!.body).toEqual(first!.body);
    await until(() => claims(h.fake).length >= 2);
    expect(fails(h.fake)).toHaveLength(2);
  });

  it("gives up after a bounded number of tries", async () => {
    const h = await start([503, 502, 500, 500, 500]);
    await h.lose();
    await until(() => claims(h.fake).length >= 2);
    expect(fails(h.fake)).toHaveLength(3);
  });

  it("never repeats after a conflict: the exit was recorded or the claim is gone", async () => {
    const h = await start([409]);
    await h.lose();
    await until(() => claims(h.fake).length >= 2);
    expect(fails(h.fake)).toHaveLength(1);
  });

  it("a plain reply is posted as the attempt's progress before the exit", async () => {
    const h = await start([]);
    await h.adapter.deliver("local", h.thread(), { kind: "chat", content: "halfway there" });
    await until(() => h.fake.calls.some((c) => c.path === "/tasks/t1/progress"));
    await h.lose();
    await until(() => fails(h.fake).length === 1);
    const order = h.fake.calls.map((c) => c.path);
    const note = h.fake.calls.find((c) => c.path === "/tasks/t1/progress");
    expect(note!.body).toMatchObject({ attempt: 2, message: "halfway there" });
    expect(order.indexOf("/tasks/t1/progress")).toBeLessThan(order.indexOf("/tasks/t1/fail"));
  });
});
