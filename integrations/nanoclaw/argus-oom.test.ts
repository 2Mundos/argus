/**
 * OOM evidence (failure-classes.spec.md 3.2.1): the channel sends `oom_kill: true` on the
 * session_lost fail only when `docker inspect` reports `State.OOMKilled` true, exit code
 * 137, for the very container it saw. Log text never sets it. Docker is a fake inspect and
 * Argus a scripted fetch; every log text is synthetic.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChannelSetup } from "./adapter.js";
import { type ArgusTask, createArgusAdapter, type RuntimeHooks } from "./argus.js";
import { type DockerRun, isOomKill, readSessionEvidence } from "./argus-stderr-tail.js";

const TASK = {
  id: "t1",
  title: "Hungry",
  spec: "Do it.",
  project: "web",
  attempt: 1,
  acceptance: [],
  knowledge_refs: [],
} as unknown as ArgusTask;

type Inspect = { stdout: string } | Error;

/** A fake `docker` answering `inspect` with `inspect` and `logs` with `logs`. */
function fakeDocker(inspect: Inspect, logs = "synthetic line\n"): DockerRun {
  return vi.fn(async (args: string[]) => {
    if (args[0] === "inspect") {
      if (inspect instanceof Error) throw inspect;
      return { stdout: inspect.stdout, stderr: "" };
    }
    return { stdout: "", stderr: logs };
  });
}

const state = (code: string, oom: string, name = "/ctr-1") => ({
  stdout: `${code}|false|${oom}|${name}\n`,
});

describe("readSessionEvidence reads OOMKilled from inspect", () => {
  it("parses the structured state and strips the name's leading slash", async () => {
    const run = fakeDocker(state("137", "true"));
    const ev = await readSessionEvidence(run, "ctr-1");
    expect(ev).toMatchObject({ exitCode: 137, oomKilled: true, container: "ctr-1" });
    expect(run).toHaveBeenCalledWith([
      "inspect",
      "--format",
      "{{.State.ExitCode}}|{{.State.Running}}|{{.State.OOMKilled}}|{{.Name}}",
      "ctr-1",
    ]);
    expect(isOomKill(ev, "ctr-1")).toBe(true);
  });

  it("leaves the fields absent when inspect fails or answers something else", async () => {
    for (const inspect of [new Error("no such container"), { stdout: "137|false|<no value>|" }]) {
      const ev = await readSessionEvidence(fakeDocker(inspect), "ctr-1");
      expect(ev.oomKilled).toBeUndefined();
      expect(ev.container).toBeUndefined();
      expect(isOomKill(ev, "ctr-1")).toBe(false);
    }
  });

  it("never takes it from the log text", async () => {
    const logs = "Out of memory: Killed process 42\nOOMKilled=true\n";
    const ev = await readSessionEvidence(fakeDocker(state("137", "false"), logs), "ctr-1");
    expect(ev.logTail).toContain("Out of memory");
    expect(ev.oomKilled).toBe(false);
    expect(isOomKill(ev, "ctr-1")).toBe(false);
  });

  it("needs all three: OOMKilled, exit 137 and the same container", () => {
    const base = { logTail: "", exitCode: 137, oomKilled: true, container: "ctr-1" };
    expect(isOomKill(base, "ctr-1")).toBe(true);
    expect(isOomKill({ ...base, oomKilled: false }, "ctr-1")).toBe(false);
    expect(isOomKill({ ...base, exitCode: 1 }, "ctr-1")).toBe(false);
    expect(isOomKill({ ...base, exitCode: null }, "ctr-1")).toBe(false);
    expect(isOomKill({ ...base, container: "other" }, "ctr-1")).toBe(false);
    expect(isOomKill({ logTail: "", exitCode: 137 }, "ctr-1")).toBe(false);
  });
});

// The channel end to end: watchSession sees ctr-1, loses it, inspects it and fails.

function fakeArgus(failStatuses: number[]) {
  const calls: Array<{ path: string; body: any }> = [];
  const queue = [TASK];
  const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });
  const fetchImpl = (async (url: string, init: RequestInit) => {
    const path = new URL(url).pathname.replace(/^\/api/, "");
    const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ path, body });
    if (path === "/agents/register")
      return json(201, { token: "t", agent: { id: "ag" }, heartbeat_interval_s: 1 });
    if (path === "/agents/me/heartbeat") return json(200, { state: "active", renewed: [] });
    if (path === "/runtime/prepare") return json(200, { state: "disabled", reason: "test" });
    if (path === "/tasks/t1/artifacts") return json(201, { id: "art-1" });
    if (path === "/tasks/t1/fail") {
      const status = failStatuses.shift() ?? 200;
      if (status >= 400) return new Response("upstream down", { status });
      return json(200, { id: "t1", state: "failed" });
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

let clock = Date.UTC(2026, 9, 31, 12);
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

/** Run one claim whose session ctr-1 vanishes; `docker` answers the evidence read. */
async function loseSession(docker: DockerRun, failStatuses: number[] = []) {
  const fake = fakeArgus(failStatuses);
  let containers = ["ctr-1"];
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
    sessionEvidence: (id) => readSessionEvidence(docker, id),
    sessionActivityAt: async () => null,
    workspaceState: async () => "ok",
  };
  let inbound = 0;
  const setup = {
    onInbound: vi.fn(async () => {
      inbound++;
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
  });
  live.push(adapter);
  await adapter.setup(setup);
  await until(() => inbound === 1);
  const beats = () => fake.calls.filter((c) => c.path === "/agents/me/heartbeat");
  const sessionBeat = async (s: string) => {
    const seen = beats().length;
    await until(() =>
      beats()
        .slice(seen)
        .some((c) => c.body?.leases?.[0]?.observation?.session === s),
    );
  };
  await sessionBeat("running");
  containers = [];
  await sessionBeat("gone");
  clock += 91_000;
  const fails = () => fake.calls.filter((c) => c.path === "/tasks/t1/fail");
  const claims = () => fake.calls.filter((c) => c.path === "/tasks/claim");
  await until(() => claims().length >= 2, 15_000);
  return fails().map((c) => c.body);
}

describe("the session_lost fail carries oom_kill only for a kernel OOM kill", () => {
  it("positive control: OOMKilled=true, exit 137, the seen container", async () => {
    const [fail, ...rest] = await loseSession(fakeDocker(state("137", "true")));
    expect(rest).toHaveLength(0);
    expect(fail).toMatchObject({ attempt: 1, reason: "session_lost", oom_kill: true });
    expect(fail.error).toMatch(/^session container ctr-1 exited \(code 137\)/);
  });

  it("repeats oom_kill with the report until Argus records it", async () => {
    const fails = await loseSession(fakeDocker(state("137", "true")), [503, 502]);
    expect(fails).toHaveLength(3);
    for (const f of fails) expect(f).toEqual(fails[0]);
    expect(fails[0]).toMatchObject({ oom_kill: true });
  }, 20_000);

  const negatives: Array<[string, Inspect, string?]> = [
    ["137 with OOMKilled=false", state("137", "false")],
    ["a plain SIGKILL (docker kill)", state("137", "false")],
    ["a user stop (SIGTERM)", state("143", "false")],
    ["OOMKilled=true with another exit code", state("1", "true")],
    ["an inspect timeout", Object.assign(new Error("Command failed"), { killed: true })],
    ["an unknown status", { stdout: "|false|<no value>|/ctr-1" }],
    ["an inspect failure", new Error("Error: No such object: ctr-1")],
    ["log text saying Out of memory", state("137", "false"), "Out of memory: Killed process"],
    ["log text saying OOMKilled", state("1", "false"), '"OOMKilled": true'],
    ["a foreign container id", state("137", "true", "/someone-else")],
  ];
  for (const [name, inspect, logs] of negatives) {
    it(`never for ${name}`, async () => {
      const [fail, ...rest] = await loseSession(fakeDocker(inspect, logs));
      expect(rest).toHaveLength(0);
      expect(fail).toMatchObject({ attempt: 1, reason: "session_lost" });
      expect(fail).not.toHaveProperty("oom_kill");
    }, 20_000);
  }
});
