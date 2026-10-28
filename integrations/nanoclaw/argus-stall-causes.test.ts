/**
 * Stall causes (stall-visibility.spec.md 3.7): the heartbeat tells Argus what the host
 * sees for the claimed lease, and a workspace that stays gone blocks the attempt.
 *
 * Incident: hosts kept heartbeating while their agent group directories had been
 * deleted, so sessions never started and the claims sat quiet until the stall
 * threshold spent an attempt each. A missing workspace now blocks
 * `env_workspace_missing` after the session grace (no attempt spent); a probe that
 * cannot tell, or a delegate that is still active, never triggers anything. These
 * tests drive the real channel with a scripted Argus and fake runtime hooks.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChannelSetup } from "./adapter.js";
import {
  type ArgusConfig,
  type ArgusTask,
  createArgusAdapter,
  type RuntimeHooks,
} from "./argus.js";

const TASK = {
  id: "t1",
  title: "Deploy the app",
  spec: "Ship v1.4 to staging.",
  project: "web",
  attempt: 1,
  acceptance: ["smoke tests pass"],
  knowledge_refs: [],
} as unknown as ArgusTask;

type Call = { path: string; body: any };

function fakeArgus() {
  const calls: Call[] = [];
  const queue = [TASK];
  const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });
  const fetchImpl = (async (url: string, init: RequestInit) => {
    const path = new URL(url).pathname.replace(/^\/api/, "");
    const body = init.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ path, body });
    if (path === "/agents/register")
      return json(201, { token: "t", agent: { id: "ag" }, heartbeat_interval_s: 1 });
    if (path === "/agents/me/heartbeat")
      return json(200, { state: "active", renewed: [], rejected: [] });
    // Run on the base image: no project runtime to prepare.
    if (path === "/runtime/prepare") return json(200, { state: "disabled", reason: "test" });
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

/** The host as the hooks see it; each test edits these. */
type Host = {
  workspace: () => Promise<"ok" | "missing" | "unknown">;
  sessions: () => Promise<string[]>;
  activity: () => Promise<Date | null>;
};

let clock = Date.UTC(2026, 9, 27, 12);

function harness(host: Host, over: Partial<ArgusConfig> = {}) {
  const fake = fakeArgus();
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
    sessions: () => host.sessions(),
    sessionEvidence: async () => ({ exitCode: 137, logTail: "" }),
    sessionActivityAt: () => host.activity(),
    workspaceState: () => host.workspace(),
  };
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
    capabilities: ["python"],
    name: "worker",
    fetch: fake.fetchImpl,
    claimWaitS: 1,
    observe: async () => ({ artifacts: [] }),
    progressIntervalMs: 0,
    now: () => clock,
    runtime,
    ...over,
  });
  live.push(adapter);
  return { fake, adapter, setup, inbound };
}

const until = async (cond: () => boolean, ms = 4000) => {
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

const beats = (f: ReturnType<typeof fakeArgus>) =>
  f.calls.filter((c) => c.path === "/agents/me/heartbeat").map((c) => c.body);
const writes = (f: ReturnType<typeof fakeArgus>, kind: string) =>
  f.calls.filter((c) => c.path === `/tasks/t1/${kind}`);
/** Wait for a heartbeat sent after now whose lease observation matches. */
async function nextBeat(f: ReturnType<typeof fakeArgus>, match: Record<string, string> = {}) {
  const seen = beats(f).length;
  await until(() =>
    beats(f)
      .slice(seen)
      .some((b) => Object.entries(match).every(([k, v]) => b.leases[0]?.observation?.[k] === v)),
  );
  return beats(f).at(-1);
}

const alive = async () => ["ctr-1"];

async function start(host: Host) {
  const h = harness(host);
  await h.adapter.setup(h.setup);
  await until(() => h.inbound.length === 1);
  return h;
}

describe("argus stall causes", () => {
  it("sends the lease observation on each heartbeat", async () => {
    const host: Host = {
      workspace: async () => "ok",
      sessions: alive,
      activity: async () => new Date(clock - 1000),
    };
    const h = await start(host);
    const beat = await nextBeat(h.fake, { session: "running" });
    expect(beat.leases).toEqual([
      {
        task_id: "t1",
        attempt: 1,
        activity_at: new Date(clock - 1000).toISOString(),
        observation: { workspace: "ok", session: "running", delegate: "active" },
      },
    ]);
  }, 15_000);

  it("blocks env_workspace_missing only after the grace, spending nothing", async () => {
    let workspace: "ok" | "missing" = "ok";
    const h = await start({
      workspace: async () => workspace,
      sessions: alive,
      activity: async () => null,
    });
    await nextBeat(h.fake, { workspace: "ok", session: "running" });
    workspace = "missing";
    await nextBeat(h.fake, { workspace: "missing" });
    clock += 89_000;
    await nextBeat(h.fake, { workspace: "missing" });
    expect(writes(h.fake, "block")).toEqual([]);
    clock += 2_000;
    await until(() => writes(h.fake, "block").length === 1);
    const [block] = writes(h.fake, "block");
    expect(block!.body).toMatchObject({ attempt: 1, reason: "env_workspace_missing" });
    expect(block!.body.error).toMatch(
      /agent group directory \(workspace\) is missing.*heartbeat alive/,
    );
    expect(writes(h.fake, "fail")).toEqual([]); // not a failure: no attempt spent
    // The claim is let go: the worker asks for its next task.
    await until(() => h.fake.calls.filter((c) => c.path === "/tasks/claim").length === 2);
  }, 15_000);

  it("does not block when the workspace comes back inside the grace", async () => {
    let workspace: "ok" | "missing" = "missing";
    const h = await start({
      workspace: async () => workspace,
      sessions: alive,
      activity: async () => null,
    });
    await nextBeat(h.fake, { workspace: "missing" });
    clock += 60_000;
    workspace = "ok";
    await nextBeat(h.fake, { workspace: "ok" });
    workspace = "missing";
    await nextBeat(h.fake, { workspace: "missing" });
    clock += 60_000; // 120 s since it first went missing, 60 s since it came back
    await nextBeat(h.fake, { workspace: "missing" });
    expect(writes(h.fake, "block")).toEqual([]);
  }, 15_000);

  it("treats probe errors as unknown: no action, and a running grace is not reset", async () => {
    let workspace: () => Promise<"ok" | "missing" | "unknown"> = async () => {
      throw new Error("EACCES: permission denied");
    };
    const h = await start({
      workspace: () => workspace(),
      sessions: async () => {
        throw new Error("docker: connection refused");
      },
      activity: async () => {
        throw new Error("EIO");
      },
    });
    const beat = await nextBeat(h.fake, { workspace: "unknown" });
    expect(beat.leases[0].observation).toEqual({
      workspace: "unknown",
      session: "unknown",
      delegate: "unknown",
    });
    clock += 600_000;
    await nextBeat(h.fake, { workspace: "unknown" });
    expect(writes(h.fake, "block")).toEqual([]);
    expect(writes(h.fake, "fail")).toEqual([]);
    // missing, then unknown for a while, then missing again: the grace kept running.
    workspace = async () => "missing";
    await nextBeat(h.fake, { workspace: "missing" });
    clock += 60_000;
    workspace = async () => "unknown";
    await nextBeat(h.fake, { workspace: "unknown" });
    clock += 31_000;
    expect(writes(h.fake, "block")).toEqual([]);
    workspace = async () => "missing";
    await until(() => writes(h.fake, "block").length === 1);
  }, 15_000);

  it("leaves a silent but active delegate alone", async () => {
    let lastSignal = clock;
    let workspace: "ok" | "missing" = "ok";
    const h = await start({
      workspace: async () => workspace,
      sessions: alive,
      activity: async () => new Date(lastSignal),
    });
    // No progress for Argus, but the session heartbeat keeps moving.
    clock += 30 * 60_000;
    lastSignal = clock - 10_000;
    await nextBeat(h.fake, { workspace: "ok", session: "running", delegate: "active" });
    workspace = "missing"; // even then: an active delegate is never interrupted
    await nextBeat(h.fake, { workspace: "missing", delegate: "active" });
    clock += 120_000;
    lastSignal = clock - 10_000;
    await nextBeat(h.fake, { workspace: "missing", delegate: "active" });
    expect(writes(h.fake, "block")).toEqual([]);
    expect(writes(h.fake, "fail")).toEqual([]);
    workspace = "ok";
    clock += 6 * 60_000; // no signal for six minutes: silent
    await nextBeat(h.fake, { workspace: "ok", session: "running", delegate: "silent" });
    expect(writes(h.fake, "block")).toEqual([]);
  }, 15_000);

  it("keeps a dead session container on the existing session_lost path", async () => {
    let containers = ["ctr-1"];
    const h = await start({
      workspace: async () => "ok",
      sessions: async () => containers,
      activity: async () => null,
    });
    await nextBeat(h.fake, { session: "running" });
    containers = [];
    await nextBeat(h.fake, { workspace: "ok", session: "gone" });
    clock += 91_000;
    await until(() => writes(h.fake, "fail").length === 1);
    expect(writes(h.fake, "fail")[0]!.body.reason).toBe("session_lost");
    expect(writes(h.fake, "block")).toEqual([]);
  }, 15_000);
});
