/**
 * Reply fencing: output of an earlier task must never apply to the task claimed next.
 *
 * Incident: task A asked for assistance, the same worker claimed unrelated task B, and A's
 * delayed `COMPLETE:` (with A's checklist ids) was posted as B's result because `deliver`
 * judged every reply against whatever was claimed at that moment. These tests replay that
 * sequence against the real channel with a scripted Argus; no live task is touched.
 */
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChannelSetup } from "./adapter.js";
import {
  type ArgusConfig,
  type ArgusTask,
  createArgusAdapter,
  fenceReply,
  foreignChecklistIds,
  parseReply,
} from "./argus.js";

const base: ArgusTask = JSON.parse(
  fs.readFileSync(
    fileURLToPath(new URL("./fixtures/continuation/ca2-resume.json", import.meta.url)),
    "utf8",
  ),
);

function mk(id: string, itemId: string): ArgusTask {
  return {
    ...base,
    id,
    attempt: 1,
    title: `work of ${id}`,
    continuation: {
      ...base.continuation!,
      artifacts: [],
      wip: null,
      interruption: null,
      checklist: [{ id: itemId, text: `criterion of ${id}`, status: "pending", evidence: null }],
      pending: [itemId],
    },
  };
}

const A = mk("task-a", "ac-a1");
const B = mk("task-b", "ac-b1");

type Call = { path: string; body: any };

function fakeArgus(
  queue: ArgusTask[],
  opts: { rejectHeartbeatFor?: () => string | null; routes?: Record<string, () => Response> } = {},
) {
  const calls: Call[] = [];
  const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });
  const fetchImpl = (async (url: string, init: RequestInit) => {
    const path = new URL(url).pathname.replace(/^\/api/, "");
    const body = init.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ path, body });
    if (opts.routes?.[path]) return opts.routes[path]!();
    if (path === "/agents/register")
      return json(201, { token: "t", agent: { id: "ag" }, heartbeat_interval_s: 1 });
    if (path === "/agents/me/heartbeat") {
      const lost = opts.rejectHeartbeatFor?.();
      return json(200, {
        state: "active",
        renewed: [],
        rejected: lost ? [{ task_id: lost }] : [],
      });
    }
    if (path === "/agents/me/leave") return json(200, {});
    if (path === "/tasks/claim") {
      const next = queue.shift();
      if (next) return json(200, next);
      return new Promise<Response>((_, reject) =>
        init.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        ),
      );
    }
    if (/\/artifacts/.test(path)) return json(201, { data: { id: "art-1" } });
    return json(200, {});
  }) as unknown as typeof fetch;
  return { calls, fetchImpl };
}

function harness(fake: ReturnType<typeof fakeArgus>, over: Partial<ArgusConfig> = {}) {
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
    ...over,
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

const reply = (text: string, extra: Record<string, unknown> = {}) =>
  ({ kind: "chat", content: { text }, ...extra }) as any;
const paths = (f: ReturnType<typeof fakeArgus>) => f.calls.map((c) => c.path);
/** What would change B's record: everything under /tasks/task-b/ except the claim's own preflight observation. */
const writesTo = (f: ReturnType<typeof fakeArgus>, id: string) =>
  paths(f).filter((p) => p.startsWith(`/tasks/${id}/`) && !p.endsWith("/observations"));
const staleResult = JSON.stringify({
  summary: "PR101 reconciliation prepared",
  checklist: [{ id: "ac-a1", status: "done", evidence: "PR 101 head 1565536" }],
});

/** A asks for assistance, the worker is given B; returns once B has been injected. */
async function switchToB(over: Partial<ArgusConfig> = {}) {
  const fake = fakeArgus([A, B]);
  const h = harness(fake, over);
  live.push(h.adapter);
  await h.adapter.setup(h.setup);
  await until(() => h.inbound.length === 1);
  await h.adapter.deliver("local", null, reply("ASSIST: prometheus_client: not installed"));
  expect(paths(fake)).toContain("/tasks/task-a/ask");
  await until(() => h.inbound.some((m) => m.id === "argus-task-b-1"));
  return { fake, h };
}

describe("a delayed result of task A after task B was claimed", () => {
  it("is not applied to B (the incident sequence)", async () => {
    const { fake, h } = await switchToB();
    await h.adapter.deliver("local", null, reply(`COMPLETE: ${staleResult}`));
    expect(writesTo(fake, "task-b")).toEqual([]);
    // Nothing completed A either: its claim had ended with the question.
    expect(paths(fake)).not.toContain("/tasks/task-a/complete");
    // B is asked to scope its replies instead, and still holds its claim.
    const reminder = h.inbound.at(-1).content.text as string;
    expect(reminder).toContain("COMPLETE on task-b:");
  });

  it("is not applied to B when it names A explicitly", async () => {
    const { fake, h } = await switchToB();
    await h.adapter.deliver("local", null, reply(`COMPLETE on task-a: ${staleResult}`));
    expect(writesTo(fake, "task-b")).toEqual([]);
    expect(paths(fake)).not.toContain("/tasks/task-a/complete");
  });

  it("does not leak progress, failure or questions of A into B either", async () => {
    const { fake, h } = await switchToB();
    for (const text of [
      "Still preparing the PR 101 branch",
      "FAIL: could not push",
      "ASK: which base branch?",
      "ASSIST: bridge: fetch failed",
      "PROGRESS on task-a: 90%",
    ])
      await h.adapter.deliver("local", null, reply(text));
    expect(writesTo(fake, "task-b")).toEqual([]);
  });

  it("drops an unscoped reply that the host stamped as an answer to A's message", () => {
    const claim = { id: "task-b", attempt: 1, scopeRequired: false };
    expect(fenceReply(claim, "COMPLETE: {}", "argus-task-a-1")).toEqual({
      ok: false,
      reason: "stale_message",
    });
    expect(fenceReply(claim, "COMPLETE: {}", "argus-task-b-2")).toEqual({
      ok: false,
      reason: "stale_message",
    }); // an earlier attempt of the same task
    expect(fenceReply(claim, "COMPLETE: {}", "argus-task-b-1-r2").ok).toBe(true);
    expect(fenceReply(claim, "COMPLETE: {}", "argus-steer-s1").ok).toBe(true);
  });
});

describe("B's own replies after the switch", () => {
  it("complete B when scoped to it, with B's checklist ids", async () => {
    const { fake, h } = await switchToB();
    const own = JSON.stringify({
      summary: "B done",
      checklist: [{ id: "ac-b1", status: "done", evidence: "receipt" }],
    });
    await h.adapter.deliver("local", null, reply(`COMPLETE on task-b: ${own}`));
    const done = fake.calls.find((c) => c.path === "/tasks/task-b/complete")!;
    expect(done.body).toMatchObject({
      attempt: 1,
      result: { summary: "B done", checklist: [{ id: "ac-b1" }] },
    });
  });

  it("send progress when scoped to B", async () => {
    const { fake, h } = await switchToB();
    await h.adapter.deliver("local", null, reply("PROGRESS on task-b: checking the ledger"));
    await until(() => paths(fake).includes("/tasks/task-b/progress"));
    expect(fake.calls.find((c) => c.path === "/tasks/task-b/progress")!.body.message).toBe(
      "checking the ledger",
    );
  });

  it("are rejected when scoped to B but carrying A's checklist ids", async () => {
    const { fake, h } = await switchToB();
    await h.adapter.deliver("local", null, reply(`COMPLETE on task-b: ${staleResult}`));
    expect(paths(fake)).not.toContain("/tasks/task-b/complete");
    expect(h.inbound.at(-1).content.text).toContain("ac-a1 do not belong to task task-b");
    // The claim is intact: a corrected completion goes through.
    await h.adapter.deliver(
      "local",
      null,
      reply(
        `COMPLETE on task-b: ${JSON.stringify({ summary: "ok", checklist: [{ id: "ac-b1", status: "done", evidence: "e" }] })}`,
      ),
    );
    expect(paths(fake)).toContain("/tasks/task-b/complete");
  });

  it("the server's checklist_mismatch does not fail the attempt", async () => {
    const fake = fakeArgus([A], {
      routes: {
        "/tasks/task-a/complete": () =>
          new Response(
            JSON.stringify({ detail: { code: "checklist_mismatch", message: "not yours" } }),
            { status: 422 },
          ),
      },
    });
    const h = harness(fake);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    await h.adapter.deliver("local", null, reply('COMPLETE: {"summary":"x"}'));
    expect(paths(fake)).toContain("/tasks/task-a/complete");
    expect(paths(fake)).not.toContain("/tasks/task-a/fail");
  });
});

describe("a task switch by release", () => {
  it("fences the next claim after Argus ended the previous one", async () => {
    let lost: string | null = null;
    const fake = fakeArgus([A, B], { rejectHeartbeatFor: () => lost });
    const h = harness(fake);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    lost = "task-a"; // heartbeat: A's lease is gone
    await until(() => h.inbound.some((m) => m.id === "argus-task-b-1"), 5000);
    lost = null;
    await h.adapter.deliver("local", null, reply(`COMPLETE: ${staleResult}`));
    expect(writesTo(fake, "task-b")).toEqual([]);
  }, 10_000);
});

describe("normal continuation of one task", () => {
  it("accepts unscoped replies when the session carried no other task", async () => {
    const fake = fakeArgus([A]);
    const h = harness(fake);
    live.push(h.adapter);
    await h.adapter.setup(h.setup);
    await until(() => h.inbound.length === 1);
    expect(h.inbound[0].content.text).not.toContain("[Argus reply scope]");
    await h.adapter.deliver("local", null, reply("working on it"));
    await until(() => paths(fake).includes("/tasks/task-a/progress"));
    await h.adapter.deliver("local", null, reply(`COMPLETE: ${staleResult}`));
    expect(paths(fake)).toContain("/tasks/task-a/complete");
  });

  it("tells a later task to scope its replies, and honours them", async () => {
    const { fake, h } = await switchToB();
    const text = h.inbound.find((m) => m.id === "argus-task-b-1").content.text as string;
    expect(text).toContain("[Argus reply scope]");
    expect(text).toContain("COMPLETE on task-b:");
    await h.adapter.deliver("local", null, reply("ASK on task-b: which ledger?"));
    expect(fake.calls.find((c) => c.path === "/tasks/task-b/ask")!.body.question).toBe(
      "which ledger?",
    );
  });
});

describe("fenceReply and foreignChecklistIds", () => {
  const claim = { id: "task-b", attempt: 1, scopeRequired: true };
  it("requires scope once the session carried a task, and only for the claimed one", () => {
    expect(fenceReply(claim, "COMPLETE: x")).toEqual({
      ok: false,
      reason: "unscoped_after_switch",
    });
    expect(fenceReply(claim, "COMPLETE on task-a: x")).toEqual({ ok: false, reason: "other_task" });
    expect(fenceReply(claim, "**FAIL for task task-a:** x").ok).toBe(false);
    expect(fenceReply(claim, "FAIL on task-b: x").ok).toBe(true);
    expect(fenceReply({ ...claim, scopeRequired: false }, "just a note").ok).toBe(true);
  });
  it("parses PROGRESS as a scoped progress note only for the claimed id", () => {
    expect(parseReply("PROGRESS on task-b: 50%", "task-b")).toEqual({
      kind: "progress",
      text: "50%",
    });
    expect(parseReply("PROGRESS on task-a: 50%", "task-b").text).toBe("PROGRESS on task-a: 50%");
  });
  it("flags checklist ids the claim does not hold", () => {
    expect(foreignChecklistIds(B, [{ id: "ac-a1", status: "done" }])).toEqual(["ac-a1"]);
    expect(foreignChecklistIds(B, [{ id: "ac-b1", status: "done" }])).toEqual([]);
    expect(foreignChecklistIds({ continuation: null }, [{ id: "x", status: "done" }])).toEqual([]);
  });
});
