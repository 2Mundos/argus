/**
 * Claim isolation: two tasks alternating on one worker never see each other's work.
 *
 * Incident FL7 replayed end to end against the real channel with a scripted Argus:
 * A (attempt 1) asks for assistance, the worker is given B, B completes, and A comes back
 * as attempt 2 on a new thread. At every step the prompt, the accepted replies, the
 * uploaded files and the completion belong to the claim in flight and to no other
 * (task-continuation.spec.md, "Reply fencing"). No live task is touched.
 */
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { afterEach, expect, it, vi } from "vitest";
import type { ChannelSetup } from "./adapter.js";
import { type ArgusTask, createArgusAdapter } from "./argus.js";

const base: ArgusTask = JSON.parse(
  fs.readFileSync(
    fileURLToPath(new URL("./fixtures/continuation/ca2-resume.json", import.meta.url)),
    "utf8",
  ),
);

/** Everything that tells one task from the other in a prompt. */
const facts = (id: string, word: string) => ({
  id,
  title: `${word} title`,
  spec: `Reconcile the ${word} ledger.`,
  criterion: `${word} criterion holds`,
  item: `ac-${word}`,
  ref: `task/${word}-branch`,
});
const FA = facts("task-a", "alpha");
const FB = facts("task-b", "beta");

function mk(f: typeof FA, attempt: number, interruption: unknown = null): ArgusTask {
  return {
    ...base,
    id: f.id,
    attempt,
    title: f.title,
    spec: f.spec,
    acceptance: [f.criterion],
    continuation: {
      ...base.continuation!,
      objective: f.spec,
      artifacts: [
        {
          attempt: 1,
          head_sha: null,
          kind: "branch",
          note: null,
          observed_at: "2026-01-01T00:00:11Z",
          provenance: "worker_reported",
          ref: f.ref,
          state: "open",
          url: null,
        },
      ],
      wip: null,
      interruption: interruption as any,
      checklist: [{ id: f.item, text: f.criterion, status: "pending", evidence: null }],
      pending: [f.item],
    },
  };
}

const A1 = mk(FA, 1);
const B1 = mk(FB, 1);
const A2 = mk(FA, 2, {
  at: "2026-01-01T00:00:22Z",
  attempt: 1,
  detail: "recycle",
  reason: "released",
});

type Call = { path: string; query: URLSearchParams; body: any };

function fakeArgus(queue: ArgusTask[]) {
  const calls: Call[] = [];
  let uploads = 0;
  const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });
  const fetchImpl = (async (url: string, init: RequestInit) => {
    const u = new URL(url);
    const path = u.pathname.replace(/^\/api/, "");
    const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ path, query: u.searchParams, body });
    if (path === "/agents/register")
      return json(201, { token: "t", agent: { id: "ag" }, heartbeat_interval_s: 1 });
    if (path === "/agents/me/heartbeat") return json(200, { state: "active", renewed: [] });
    if (path === "/tasks/claim") {
      const next = queue.shift();
      if (next) return json(200, next);
      return new Promise<Response>((_, reject) =>
        init.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        ),
      );
    }
    if (/\/artifacts$/.test(path)) return json(201, { id: `art-${++uploads}` });
    return json(200, {});
  }) as unknown as typeof fetch;
  return { calls, fetchImpl };
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

const reply = (text: string, files?: Array<{ filename: string; data: Buffer }>) =>
  ({ kind: "chat", content: { text }, ...(files ? { files } : {}) }) as any;
const result = (item: string) =>
  JSON.stringify({ summary: "done", checklist: [{ id: item, status: "done", evidence: "e" }] });
const report = () => [{ filename: "report.md", data: Buffer.from("# identical report\n") }];

it("keeps prompts, replies, files and checklists of A and B apart across A's requeue", async () => {
  const fake = fakeArgus([A1, B1, A2]);
  const inbound: any[] = [];
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
  const adapter = createArgusAdapter({
    url: "http://argus.test",
    bootstrapToken: "boot",
    capabilities: ["python"],
    name: "worker",
    fetch: fake.fetchImpl,
    claimWaitS: 1,
    observe: async () => ({ artifacts: [] }),
    progressIntervalMs: 0,
  });
  live.push(adapter);

  const say = (t: string | null, text: string, files?: ReturnType<typeof report>) =>
    adapter.deliver("local", t, reply(text, files));
  const prompt = (id: string) => inbound.find((m) => m.id === id).content.text as string;
  /** Task writes a leaked reply would make (the claim's own observation and trace excepted). */
  const writes = () =>
    fake.calls
      .map((c) => c.path)
      .filter(
        (p) =>
          /^\/tasks\/task-[ab]\//.test(p) && !p.endsWith("/observations") && !p.endsWith("/models"),
      );
  const uploads = () =>
    fake.calls
      .filter((c) => c.path.endsWith("/artifacts"))
      .map((c) => `${c.path} attempt=${c.query.get("attempt")} ${c.query.get("name")}`);
  /** (a) the prompt names its own task and nothing of the other. */
  const ownsOnly = (text: string, own: typeof FA, other: typeof FA) => {
    for (const v of Object.values(own)) expect(text).toContain(v);
    for (const v of Object.values(other)) expect(text).not.toContain(v);
  };
  /** (b) nothing said on a thread other than the claim's reaches any task. */
  const dropped = async (t: string, item: string) => {
    const before = writes().length;
    const seen = inbound.length;
    for (const text of [`COMPLETE: ${result(item)}`, "FAIL: broke", "ASK: which?", "progress"])
      await say(t, text, report());
    expect(writes().length).toBe(before);
    expect(inbound.length).toBe(seen);
  };

  await adapter.setup(setup);

  // A, attempt 1.
  await until(() => inbound.length === 1);
  expect(thread).toBe("argus:task-a:1");
  ownsOnly(prompt("argus-task-a-1"), FA, FB);
  await say("argus:task-a:1", "", report());
  expect(uploads()).toEqual(["/tasks/task-a/artifacts attempt=1 report.md"]);
  await say("argus:task-a:1", "ASSIST: prometheus_client: not installed");
  expect(writes().at(-1)).toBe("/tasks/task-a/ask");

  // B, attempt 1, on the same worker.
  await until(() => inbound.some((m) => m.id === "argus-task-b-1"));
  expect(thread).toBe("argus:task-b:1");
  ownsOnly(prompt("argus-task-b-1"), FB, FA);
  await dropped("argus:task-a:1", FA.item);
  // (c) The same file name and bytes A stored is B's own artifact, not a duplicate.
  await say("argus:task-b:1", "", report());
  expect(uploads().slice(1)).toEqual(["/tasks/task-b/artifacts attempt=1 report.md"]);
  // (d) A's ids on B's own thread are fenced client-side; B's own ids complete B.
  await say("argus:task-b:1", `COMPLETE: ${result(FA.item)}`);
  expect(writes()).not.toContain("/tasks/task-b/complete");
  expect(inbound.at(-1).content.text).toContain(`${FA.item} do not belong to task task-b`);
  await say("argus:task-b:1", `COMPLETE: ${result(FB.item)}`);
  const bDone = fake.calls.find((c) => c.path === "/tasks/task-b/complete")!;
  expect(bDone.body).toMatchObject({ attempt: 1, result: { checklist: [{ id: FB.item }] } });
  expect(bDone.body.result.artifacts).toEqual(["art-2"]); // B's upload only

  // A requeued: attempt 2 on a new thread.
  await until(() => inbound.some((m) => m.id === "argus-task-a-2"));
  expect(thread).toBe("argus:task-a:2");
  const a2 = prompt("argus-task-a-2");
  // (e) A's checklist and A's prior artifacts come back; nothing of B's does.
  ownsOnly(a2, FA, FB);
  expect(a2).toContain("Attempt: 2");
  await dropped("argus:task-a:1", FA.item);
  await dropped("argus:task-b:1", FB.item);
  expect(writes().filter((p) => p === "/tasks/task-a/complete")).toEqual([]);
  // The attempt-1 upload does not dedupe away the same file for attempt 2.
  await say("argus:task-a:2", "", report());
  expect(uploads().slice(2)).toEqual(["/tasks/task-a/artifacts attempt=2 report.md"]);
  await say("argus:task-a:2", `COMPLETE: ${result(FB.item)}`);
  expect(writes()).not.toContain("/tasks/task-a/complete");
  expect(inbound.at(-1).content.text).toContain(`${FB.item} do not belong to task task-a`);
  await say("argus:task-a:2", `COMPLETE: ${result(FA.item)}`);
  const aDone = fake.calls.filter((c) => c.path === "/tasks/task-a/complete");
  expect(aDone).toHaveLength(1);
  expect(aDone[0]!.body).toMatchObject({
    attempt: 2,
    result: { checklist: [{ id: FA.item }], artifacts: ["art-3"] },
  });
  // Every write went to the claim in flight at the time, in order.
  expect(writes()).toEqual([
    "/tasks/task-a/artifacts",
    "/tasks/task-a/ask",
    "/tasks/task-b/artifacts",
    "/tasks/task-b/complete",
    "/tasks/task-a/artifacts",
    "/tasks/task-a/complete",
  ]);
});
