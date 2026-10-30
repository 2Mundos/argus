/**
 * The session stderr tail (worker-lifecycle.spec.md 3.12.1): byte caps at a code-point
 * boundary, redaction before the cap, the `stderr-tail` artifact on the session_lost and
 * failure paths, and a bounded inline excerpt. Every log text here is a synthetic
 * fixture; Docker is a fake runner, and Argus a scripted fetch.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChannelSetup } from "./adapter.js";
import {
  type ArgusConfig,
  type ArgusTask,
  createArgusAdapter,
  type RuntimeHooks,
} from "./argus.js";
import {
  boundHead,
  boundTail,
  type DockerRun,
  inlineExcerpt,
  readSessionEvidence,
  redactSessionTail,
  type SessionEvidence,
  STDERR_TAIL_MAX_BYTES,
  utf8Bytes,
} from "./argus-stderr-tail.js";

const bytes = (s: string) => new TextEncoder().encode(s).length;
/** Valid UTF-8 (no U+FFFD from a cut character) and the original's suffix. */
const isCleanSuffix = (cut: string, of: string) => !cut.includes("�") && of.endsWith(cut);

describe("boundTail (synthetic text)", () => {
  it("keeps text under the cap whole", () => {
    expect(boundTail("héllo", 64)).toEqual({ text: "héllo", bytes: 6, truncated: false });
  });

  it("keeps text exactly at the cap whole", () => {
    const text = "a".repeat(14) + "€"; // 14 + 3 = 17 bytes
    expect(boundTail(text, 17)).toEqual({ text, bytes: 17, truncated: false });
  });

  it("drops a multibyte character cut at the front, never exceeding the cap", () => {
    const text = `${"x".repeat(10)}€€€`; // 10 + 9 bytes
    // The last 8 bytes start inside the first euro sign: it is dropped, not split.
    const cut = boundTail(text, 8);
    expect(cut).toEqual({ text: "€€", bytes: 6, truncated: true });
  });

  it("never splits an emoji's surrogate pair", () => {
    const text = `start ${"😀".repeat(10)}`; // 4 bytes, 2 code units each
    for (let cap = 1; cap <= 30; cap++) {
      const cut = boundTail(text, cap);
      expect(cut.bytes).toBeLessThanOrEqual(cap);
      expect(cut.bytes).toBe(bytes(cut.text));
      expect(cut.bytes % 4).toBe(0);
      expect(isCleanSuffix(cut.text, text)).toBe(true);
      expect(/[\uD800-\uDBFF]$|^[\uDC00-\uDFFF]/.test(cut.text)).toBe(false);
    }
  });

  it("the head cut also stops at a code point", () => {
    expect(boundHead("ab😀cd", 4)).toBe("ab");
    expect(boundHead("ab😀cd", 6)).toBe("ab😀");
  });
});

describe("redaction before the cap (synthetic secrets)", () => {
  it("a token straddling the cap boundary is redacted, not leaked", () => {
    const secret = ["sk", "-SYNTHETICSECRET0123456789abcdef"].join("");
    const text = `${"l".repeat(100)} ${secret} done`;
    // A cap that would start inside the token: its `sk-` prefix falls outside.
    const cap = bytes(" done") + secret.length - 5;
    const capThenRedact = redactSessionTail(boundTail(text, cap).text);
    expect(capThenRedact).toContain("SECRET"); // the wrong order leaks the rest of it
    const tail = boundTail(redactSessionTail(text), cap).text;
    expect(tail).not.toContain("SECRET");
    expect(tail).not.toContain("0123456789");
  });

  it("readSessionEvidence redacts the merged logs before the 16 KiB cap", async () => {
    const secret = ["ghp", "_SYNTHETICSECRETxxxxxxxxxxxxxxxx"].join("");
    const filler = "f".repeat(STDERR_TAIL_MAX_BYTES - 20);
    const run: DockerRun = async (args) =>
      args[0] === "logs"
        ? { stdout: `${secret}${filler}`, stderr: "" }
        : { stdout: "1|false", stderr: "" };
    const ev = await readSessionEvidence(run, "ctr-1");
    expect(ev.logTail).not.toMatch(/SYNTHETIC|SECRETx/);
    expect(utf8Bytes(ev.logTail)).toBeLessThanOrEqual(STDERR_TAIL_MAX_BYTES);
  });
});

describe("readSessionEvidence (fake docker, synthetic logs)", () => {
  const fakeDocker = (logs: { stdout: string; stderr: string } | Error): DockerRun =>
    vi.fn(async (args: string[]) => {
      if (args[0] === "inspect") return { stdout: "137|false\n", stderr: "" };
      if (logs instanceof Error) throw logs;
      return logs;
    });

  it("captures both stdout and stderr", async () => {
    const run = fakeDocker({
      stdout: "synthetic stdout line\n",
      stderr: "synthetic stderr: fatal\n",
    });
    const ev = await readSessionEvidence(run, "ctr-1");
    expect(ev.exitCode).toBe(137);
    expect(ev.logTail).toBe("synthetic stdout line\nsynthetic stderr: fatal");
    expect(ev).toMatchObject({ truncated: false, complete: true });
    expect(run).toHaveBeenCalledWith(["logs", "--tail", "200", "ctr-1"]);
  });

  it("is incomplete when docker returned as many lines as --tail allows", async () => {
    const ev = await readSessionEvidence(fakeDocker({ stdout: "a\nb\n", stderr: "c\n" }), "x", 3);
    expect(ev).toMatchObject({ truncated: false, complete: false });
  });

  it("is truncated and incomplete past 16 KiB", async () => {
    const big = `${"€".repeat(STDERR_TAIL_MAX_BYTES)}`; // 3 bytes each
    const ev = await readSessionEvidence(fakeDocker({ stdout: "", stderr: big }), "x");
    expect(ev).toMatchObject({ truncated: true, complete: false });
    expect(utf8Bytes(ev.logTail)).toBeLessThanOrEqual(STDERR_TAIL_MAX_BYTES);
    expect(isCleanSuffix(ev.logTail, big)).toBe(true);
  });

  it("an unreadable log is empty and incomplete", async () => {
    const ev = await readSessionEvidence(fakeDocker(new Error("no such container")), "x");
    expect(ev).toEqual({ exitCode: 137, logTail: "", truncated: false, complete: false });
  });
});

describe("inlineExcerpt", () => {
  it("stays within 2048 bytes with a multibyte tail, keeping head and pointer", () => {
    const tail = "日本語のログ".repeat(400); // synthetic, 3 bytes per character
    const out = inlineExcerpt("session container c exited; last output: ", tail, " (ptr)");
    expect(bytes(out)).toBeLessThanOrEqual(2048);
    expect(bytes(out)).toBeGreaterThan(2040);
    expect(out.startsWith("session container c exited")).toBe(true);
    expect(out.endsWith(" (ptr)")).toBe(true);
    expect(out).not.toContain("�");
  });
});

// -- the channel ------------------------------------------------------------------

const TASK = {
  id: "t1",
  title: "Deploy the app",
  spec: "Ship v1.4 to staging.",
  project: "web",
  attempt: 1,
  acceptance: ["smoke tests pass"],
  knowledge_refs: [],
} as unknown as ArgusTask;

type Call = { path: string; query: URLSearchParams; contentType: string; body: any };

function fakeArgus(opts: { uploadStatus?: number } = {}) {
  const calls: Call[] = [];
  const queue = [TASK];
  const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });
  const fetchImpl = (async (url: string, init: RequestInit) => {
    const u = new URL(url);
    const path = u.pathname.replace(/^\/api/, "");
    const raw = init.body;
    const body =
      raw instanceof Uint8Array
        ? new TextDecoder().decode(raw)
        : raw
          ? JSON.parse(raw as string)
          : undefined;
    const contentType = (init.headers as Record<string, string>)["Content-Type"] ?? "";
    calls.push({ path, query: u.searchParams, contentType, body });
    if (path === "/agents/register")
      return json(201, { token: "t", agent: { id: "ag" }, heartbeat_interval_s: 1 });
    if (path === "/agents/me/heartbeat")
      return json(200, { state: "active", renewed: [], rejected: [] });
    if (path === "/runtime/prepare") return json(200, { state: "disabled", reason: "test" });
    if (path === "/tasks/t1/artifacts" && init.method === "POST") {
      if (opts.uploadStatus) return json(opts.uploadStatus, { detail: { code: "boom" } });
      return json(201, { id: "art-1" });
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

const until = async (cond: () => boolean, ms = 4000) => {
  const start = Date.now();
  while (!cond()) {
    if (Date.now() - start > ms) throw new Error("timed out waiting for condition");
    await new Promise((r) => setTimeout(r, 5));
  }
};

async function start(
  evidence: SessionEvidence,
  sessions: () => Promise<string[]>,
  over: Partial<ArgusConfig> & { uploadStatus?: number } = {},
) {
  const fake = fakeArgus({ uploadStatus: over.uploadStatus });
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
    sessions,
    sessionEvidence: async () => evidence,
    sessionActivityAt: async () => null,
    workspaceState: async () => "ok",
  };
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
    now: () => clock,
    runtime,
    ...over,
  });
  live.push(adapter);
  await adapter.setup(setup);
  await until(() => inbound.length === 1);
  return { fake, adapter, thread: () => thread };
}

const paths = (f: ReturnType<typeof fakeArgus>) => f.calls.map((c) => `${c.path}`);
const fails = (f: ReturnType<typeof fakeArgus>) =>
  f.calls.filter((c) => c.path === "/tasks/t1/fail");
const uploads = (f: ReturnType<typeof fakeArgus>) =>
  f.calls.filter((c) => c.path === "/tasks/t1/artifacts");

/** Wait for a heartbeat sent after now that observes the session in `state`. */
async function sessionBeat(f: ReturnType<typeof fakeArgus>, state: string) {
  const beats = () => f.calls.filter((c) => c.path === "/agents/me/heartbeat");
  const seen = beats().length;
  await until(
    () =>
      beats()
        .slice(seen)
        .some((c) => c.body?.leases?.[0]?.observation?.session === state),
    8000,
  );
}

/** Synthetic: a long multibyte log whose last line is the cause. */
const LONG_TAIL = `${"ログ出力 synthetic line\n".repeat(600)}fatal: synthetic cause 😀`;

async function loseSession(over: Parameters<typeof start>[2] = {}) {
  let containers = ["ctr-1"];
  const evidence: SessionEvidence = {
    exitCode: 137,
    logTail: boundTail(LONG_TAIL, STDERR_TAIL_MAX_BYTES).text,
    truncated: true,
    complete: false,
  };
  const h = await start(evidence, async () => containers, over);
  await sessionBeat(h.fake, "running");
  containers = [];
  await sessionBeat(h.fake, "gone");
  clock += 91_000;
  await until(() => fails(h.fake).length === 1);
  return h;
}

describe("stderr-tail artifact on session_lost (synthetic logs)", () => {
  it("uploads stderr-tail with role other and metadata before a bounded fail", async () => {
    const h = await loseSession();
    const [up] = uploads(h.fake);
    expect(up).toBeDefined();
    expect(up!.query.get("name")).toBe("stderr-tail");
    expect(up!.query.get("role")).toBe("other");
    expect(up!.query.get("attempt")).toBe("1");
    const stored = up!.body as string;
    expect(up!.contentType).toBe(
      `text/plain; charset=utf-8; truncated=true; complete=false; bytes=${bytes(stored)}; redaction_version=1`,
    );
    expect(bytes(stored)).toBeLessThanOrEqual(STDERR_TAIL_MAX_BYTES);
    expect(bytes(stored)).toBeGreaterThan(2048);
    // The upload comes first, while the lease still holds.
    const order = paths(h.fake);
    expect(order.indexOf("/tasks/t1/artifacts")).toBeLessThan(order.indexOf("/tasks/t1/fail"));

    const [fail] = fails(h.fake);
    expect(fail!.body.reason).toBe("session_lost");
    const error = fail!.body.error as string;
    expect(bytes(error)).toBeLessThanOrEqual(2048);
    expect(error).toMatch(
      /^session container ctr-1 exited \(code 137\) without a reply; last output: /,
    );
    expect(error).toContain("fatal: synthetic cause 😀");
    expect(error.endsWith(" (full tail: artifact stderr-tail)")).toBe(true);
    expect(error).not.toContain("�");
    // The full tail is the artifact only: it reaches no other call (fail, heartbeat, log).
    for (const c of h.fake.calls.filter((c) => c !== up))
      expect(JSON.stringify(c.body ?? "")).not.toContain(stored.slice(0, 4000));
  }, 15_000);

  it("an upload failure does not block the fail, and the pointer is left out", async () => {
    const h = await loseSession({ uploadStatus: 500 });
    expect(uploads(h.fake)).toHaveLength(1);
    const [fail] = fails(h.fake);
    expect(fail!.body.reason).toBe("session_lost");
    expect(bytes(fail!.body.error)).toBeLessThanOrEqual(2048);
    expect(fail!.body.error).not.toContain("artifact stderr-tail");
  }, 15_000);

  it("uploads nothing when there is no tail", async () => {
    let containers = ["ctr-1"];
    const h = await start({ exitCode: 1, logTail: "" }, async () => containers);
    await sessionBeat(h.fake, "running");
    containers = [];
    await sessionBeat(h.fake, "gone");
    clock += 91_000;
    await until(() => fails(h.fake).length === 1);
    expect(uploads(h.fake)).toEqual([]);
    expect(fails(h.fake)[0]!.body.error).toBe(
      "session container ctr-1 exited (code 1) without a reply",
    );
  }, 15_000);
});

describe("stderr-tail artifact on a generic failure (synthetic log)", () => {
  const GENERIC = "The agent run failed. Check the logs for details.";

  it("stores the agent log tail and points the bounded fail error at it", async () => {
    const log = `synthetic start\n${`${"x".repeat(80)}\n`.repeat(60)}fatal: synthetic no checkout\n`;
    const h = await start({ exitCode: null, logTail: "" }, async () => ["ctr-1"], {
      agentLogTail: async () => ({
        exitCode: null,
        logTail: log,
        truncated: false,
        complete: true,
      }),
    });
    await h.adapter.deliver("local", h.thread(), {
      kind: "chat",
      content: { text: GENERIC, failureNotice: true },
    });
    await until(() => fails(h.fake).length === 1);
    const [up] = uploads(h.fake);
    expect(up!.query.get("role")).toBe("other");
    expect(up!.body).toBe(log); // stored as read (nothing to redact)
    expect(up!.contentType).toContain("truncated=false; complete=true");
    const error = fails(h.fake)[0]!.body.error as string;
    expect(error).toContain("fatal: synthetic no checkout");
    expect(error.endsWith(" (full tail: artifact stderr-tail)")).toBe(true);
    expect(bytes(error)).toBeLessThanOrEqual(2048);
    expect(error).not.toContain(log.slice(0, 1000));
  }, 15_000);

  it("a plain-string tail hook is stored as not known to be complete", async () => {
    const h = await start({ exitCode: null, logTail: "" }, async () => ["ctr-1"], {
      agentLogTail: async () => "synthetic: boom token=SYNTHSECRET1\n",
      uploadStatus: undefined,
    });
    await h.adapter.deliver("local", h.thread(), {
      kind: "chat",
      content: { text: GENERIC, failureNotice: true },
    });
    await until(() => fails(h.fake).length === 1);
    const [up] = uploads(h.fake);
    expect(up!.contentType).toContain("truncated=false; complete=false");
    expect(up!.body).not.toContain("SYNTHSECRET1");
  }, 15_000);
});
