/**
 * Repository preflight session, offline. The runner is exercised against NanoClaw's real
 * `DockerSessionDriver` (real spec validation and argv) and real `composeSessionSpec`, with a
 * fake docker CLI; the "container" is simulated by writing the verdict file the helper would
 * write. These tests prove the host's control flow (deadlines, cancellation, teardown, what is
 * accepted). They are NOT evidence of forge access or of OneCLI scoping; the real-container
 * test is argus-repo-preflight.docker.test.ts and the real-forge proof is the rollout canary.
 */
import {
  existsSync,
  linkSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  symlinkSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { composeSessionSpec } from "../container-runner.js";
import { DockerSessionDriver } from "../drivers/docker-driver.js";
import { FakeCli } from "../drivers/fake-cli.js";
import type { RepositoryAssignmentCheck } from "./argus-accounts.js";
import {
  assertPreflightPositive,
  ephemeralSessionRunner,
  type GatewayControl,
  type PreflightEvidenceEvent,
  prepareResultDir,
  RepositoryPreflightError,
  readVerdictFile,
  runRepositoryPreflight,
  safeText,
  sessionRepositoryProbe,
  sweepPreflightResidue,
} from "./argus-repo-preflight.js";
import { main as giteaMain } from "./gitea-api.mjs";
import { main as githubMain } from "./github-api.mjs";

vi.mock("../log.js", () => ({
  log: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn(), fatal: vi.fn() },
}));

const GITHUB = "https://github.com/acme/widget";
const GITEA = "https://git.example.test/acme/widget";
const PROXY = "http://onecli.test:10255";

const verdict = (provider: string, repository: string, extra: object = {}) => ({
  ok: true,
  provider,
  repository,
  authenticated: true,
  write: true,
  read: true,
  allowlist_refuses_other_host: true,
  allowlist_refuses_sibling: true,
  ...extra,
});

type Op = "group" | "ensure" | "compose" | "prepare" | "start" | "status" | "stop" | "release";
type Child = { code: number | null; verdict?: unknown } | "hang";

let root: string;
let cli: FakeCli;
let released: number;
let ensured: number;
let hangBefore: Set<Op>; // the operation does not run until the test lets it
let hangAfter: Set<Op>; // the operation runs; its result arrives only when the test lets it
let pending: Array<() => void>;
let stillListed: boolean;
let preparedId: string;
let child: (resultDir: string, args: string[]) => Child;
let gatewayEnv: Record<string, string>;
let gatewayMounts: Array<Record<string, string>>;
let profile: string | undefined;
let startupGate: Promise<void> | undefined;
let statusScript: Array<{ phase: string; failure?: object }>;
let reportUnavailable: ((reason: string) => void) | undefined;
let unavailableAtExit: string | undefined;
let releaseFails: boolean;
let prepared: Array<{ containers: Array<{ securityProfile?: string; mounts: unknown[] }> }>;

/** Run `fn` now/later/never-until-released, per the hang knobs. */
function maybe<T>(op: Op, fn: () => Promise<T>): Promise<T> {
  if (hangBefore.has(op))
    return new Promise<T>((resolve, reject) => {
      pending.push(() => void fn().then(resolve, reject));
    });
  if (hangAfter.has(op))
    return new Promise<T>((resolve, reject) => {
      const done = fn();
      pending.push(() => void done.then(resolve, reject));
    });
  return fn();
}
const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
const release = async () => {
  const all = pending.splice(0);
  for (const fn of all) fn();
  await new Promise((r) => setTimeout(r, 30));
};

function policy() {
  return {
    groupsRoot: path.join(root, "groups"),
    dataRoot: path.join(root, "data"),
    surfaceRoots: [],
    materialsRoot: path.join(root, "data", "session-materials"),
    gatewayTrustRoot: path.join(root, "data", "gateway-trust"),
  };
}

function runner(
  options: { capabilities?: object; assertSpecAllowed?: (c: unknown, s: never) => void } = {},
) {
  const driver = new DockerSessionDriver({ ...policy(), cli });
  return ephemeralSessionRunner({
    installSlug: "slug",
    startupReady: () => startupGate ?? Promise.resolve(),
    group: () => maybe("group", async () => ({ id: "group-1", name: "Argus" })),
    sessionDir: (g, s) => path.join(policy().dataRoot, "v2-sessions", g, s),
    driver: () =>
      ({
        capabilities: () => options.capabilities ?? driver.capabilities(),
        prepare: (spec: { key: { sessionId: string } }) =>
          maybe("prepare", async () => {
            preparedId = spec.key.sessionId;
            prepared.push(spec as never);
            const handle = await driver.prepare(spec as never);
            return {
              start: () => maybe("start", () => handle.start()),
              status: () =>
                maybe("status", async () =>
                  statusScript.length ? (statusScript.shift() as never) : handle.status(),
                ),
              stop: (reason: string) => maybe("stop", () => handle.stop(reason)),
            };
          }),
        listSessions: async () =>
          stillListed
            ? [{ handle: { key: { sessionId: preparedId }, stop: async () => undefined } }]
            : [],
      }) as never,
    ensureGateway: () =>
      maybe("ensure", async () => {
        ensured += 1;
        return {
          lease: {
            contribution: { env: gatewayEnv, mounts: gatewayMounts },
            onUnavailable: (report: (reason: string) => void) => {
              reportUnavailable = report;
            },
          },
          controller: new AbortController(),
        } as unknown as GatewayControl;
      }),
    releaseGateway: () =>
      maybe("release", async () => {
        released += 1;
        if (releaseFails) throw new Error("release blew up http://agent:tok@proxy.test:1");
      }),
    assertSpecAllowed: options.assertSpecAllowed,
    compose: ({ groupId, sessionId, containerName, gateway }) =>
      maybe("compose", async () => {
        const spec = composeSessionSpec({
          agentGroup: { id: groupId, name: "Argus", folder: "argus" } as never,
          session: { id: sessionId, agent_group_id: groupId, agent_provider: null } as never,
          containerName,
          mounts: [],
          containerConfig: { imageTag: "argus-agent:test" } as never,
          contribution: {} as never,
          gateway: {
            ...gateway,
            networkAccess: { endpoint: "host.docker.internal", target: { kind: "host" } },
          } as never,
          mailboxEnvironment: {},
        });
        if (profile) spec.containers[0].securityProfile = profile as never;
        return spec as never;
      }),
    sleep: async () => {
      const started = cli.started[0];
      if (!started) return;
      const create = cli.callMatching(/^create /)!.args;
      const mount = create.find((a) => a.endsWith(":/preflight"))!;
      const dir = mount.split(":")[0];
      const outcome = child(dir, create);
      if (outcome === "hang") return;
      if (outcome.verdict !== undefined) {
        mkdirSync(dir, { recursive: true });
        writeFileSync(path.join(dir, "result.json"), JSON.stringify(outcome.verdict));
      }
      if (unavailableAtExit) reportUnavailable?.(unavailableAtExit);
      started.proc.emitExit(outcome.code);
      child = () => "hang";
    },
    pollMs: 5,
    teardownStepMs: 80,
  });
}

const freshCli = () => {
  cli = new FakeCli("docker");
  cli.responses = [{ match: /^inspect /, throws: new Error("No such object") }];
};
const probe = (options: Parameters<typeof runner>[0] = {}, timeoutMs = 400) => {
  freshCli();
  released = 0;
  ensured = 0;
  return sessionRepositoryProbe(runner(options), timeoutMs);
};
const run = (p = probe()) => p(GITHUB, [GITHUB], "github");
const removed = () => cli.joined().some((c) => /^rm --force /.test(c));

beforeEach(() => {
  root = mkdtempSync(path.join(tmpdir(), "argus-preflight-"));
  hangBefore = new Set();
  hangAfter = new Set();
  pending = [];
  stillListed = false;
  preparedId = "";
  prepared = [];
  profile = undefined;
  startupGate = undefined;
  statusScript = [];
  reportUnavailable = undefined;
  unavailableAtExit = undefined;
  releaseFails = false;
  gatewayMounts = [];
  gatewayEnv = { HTTPS_PROXY: PROXY, SSL_CERT_FILE: "/tmp/onecli-combined-ca.pem" };
  child = () => ({ code: 0, verdict: verdict("github", GITHUB) });
  freshCli();
});
afterEach(() => rmSync(root, { recursive: true, force: true }));

describe("repository preflight session", () => {
  it("cold start: runs the helper as PID 1 in a model-free session of the same image and proxy", async () => {
    let seen: string[] = [];
    child = (_dir, args) => {
      seen = args;
      return { code: 0, verdict: verdict("github", GITHUB) };
    };
    await probe()(`${GITHUB}.git`, [GITHUB], "github");
    expect(seen[seen.indexOf("--entrypoint") + 1]).toBe("/usr/local/bin/argus-github-api");
    expect(seen.slice(seen.indexOf("argus-agent:test"))).toEqual([
      "argus-agent:test",
      "repo-preflight",
      "--repository",
      `${GITHUB}.git`,
      "--access",
      "write",
      "--result-file",
      "/preflight/result.json",
      "--timeout-ms",
      "400",
    ]);
    expect(seen).toContain(`HTTPS_PROXY=${PROXY}`);
    expect(seen).toContain(
      `ARGUS_REPOSITORIES_JSON=${JSON.stringify({ repository: `${GITHUB}.git` })}`,
    );
    expect(seen.join(" ")).not.toMatch(/bun run|NANOCLAW_MAILBOX/);
    expect(seen).toContain("argus-preflight=1");
    expect(removed()).toBe(true);
    expect(released).toBe(1);
  });

  it("uses the Gitea helper for a Gitea repository", async () => {
    let entry = "";
    child = (_dir, args) => {
      entry = args[args.indexOf("--entrypoint") + 1];
      return { code: 0, verdict: verdict("gitea", GITEA) };
    };
    await probe()(`${GITEA}.git`, [GITEA], "gitea");
    expect(entry).toBe("/usr/local/bin/argus-gitea-api");
  });

  it("blocks on an authentication denial and still tears the session down", async () => {
    child = () => ({
      code: 13,
      verdict: {
        ok: false,
        error: {
          code: "policy_denied",
          message: "the credential is not authenticated with write access",
        },
      },
    });
    await expect(run()).rejects.toThrow(/exited with code 13 \(helper error policy_denied\)/);
    expect(released).toBe(1);
    expect(removed()).toBe(true);
  });

  it("blocks when OneCLI configured no proxy and never creates a container", async () => {
    gatewayEnv = { SSL_CERT_FILE: "/tmp/onecli-combined-ca.pem" };
    await expect(run()).rejects.toThrow(/did not configure an HTTPS proxy/);
    expect(cli.callMatching(/^create /)).toBeUndefined();
    expect(released).toBe(1);
  });

  it("blocks on an unexpected exit with no verdict, with and without an exit code", async () => {
    child = () => ({ code: 137 });
    await expect(run()).rejects.toThrow(/exited with code 137 \(no verdict/);
    child = () => ({ code: 0 });
    await expect(run()).rejects.toThrow(/did not confirm access/);
    child = () => ({ code: null });
    await expect(run()).rejects.toThrow(/did not confirm access/);
  });

  it("refuses a verdict for another provider, repository or an unauthenticated read", async () => {
    child = () => ({ code: 0, verdict: verdict("gitea", GITHUB) });
    await expect(run()).rejects.toThrow(/another provider than github/);
    child = () => ({ code: 0, verdict: verdict("github", "https://github.com/acme/other") });
    await expect(run()).rejects.toThrow(/different repository/);
    child = () => ({ code: 0, verdict: verdict("github", GITHUB, { authenticated: false }) });
    await expect(run()).rejects.toThrow(/authenticated write access in its verdict/);
    child = () => ({
      code: 0,
      verdict: verdict("github", GITHUB, { allowlist_refuses_sibling: false }),
    });
    await expect(run()).rejects.toThrow(/helper allowlist refusals/);
  });
});

describe("what the container can put in the task's error", () => {
  const SECRET = "ghp_" + "AbCdEf0123456789".repeat(2).slice(0, 36);

  it("reports only a closed reason from the helper's verdict, never its free text", async () => {
    child = () => ({
      code: 13,
      verdict: {
        ok: false,
        error: { code: "policy_denied", message: `token ${SECRET} \u001b[31m${"x".repeat(5000)}` },
      },
    });
    const error = await run().then(
      () => null,
      (e: Error) => e,
    );
    expect(error?.message).toBe(
      "the repository preflight session exited with code 13 (helper error policy_denied)",
    );
    child = () => ({
      code: 13,
      verdict: { ok: false, error: { code: `${SECRET}\n`, message: SECRET } },
    });
    const unknown = await run().then(
      () => null,
      (e: Error) => e,
    );
    expect(unknown?.message).toMatch(/helper error of an unknown kind/);
    expect(unknown?.message).not.toContain(SECRET);
  });

  it("does not echo a container-chosen provider or repository", async () => {
    child = () => ({ code: 0, verdict: verdict(SECRET, GITHUB) });
    await expect(run()).rejects.not.toThrow(new RegExp(SECRET));
    child = () => ({ code: 0, verdict: verdict("github", SECRET) });
    await expect(run()).rejects.not.toThrow(new RegExp(SECRET));
  });

  it("scrubs and bounds text from drivers and OneCLI", () => {
    const text = safeText(`boom Authorization: Bearer ${SECRET} \u0000\u001b ${"y".repeat(500)}`);
    expect(text).not.toContain(SECRET);
    expect(text.length).toBeLessThanOrEqual(160);
    expect(text).not.toMatch(/[^\x20-\x7e]/);
  });
});

describe("nothing is created before NanoClaw's startup adoption has finished", () => {
  it("waits for it, then runs", async () => {
    let open!: () => void;
    startupGate = new Promise<void>((resolve) => {
      open = resolve;
    });
    const pending = run(probe({}, 2000));
    await new Promise((r) => setTimeout(r, 60));
    expect(ensured).toBe(0);
    expect(cli.calls).toHaveLength(0);
    open();
    await expect(pending).resolves.toMatchObject({ authentication: "unknown" });
    expect(ensured).toBe(1);
  });

  it("fails by the deadline, with no lease and no container, when it never finishes", async () => {
    startupGate = new Promise<void>(() => undefined);
    await expect(run(probe({}, 100))).rejects.toThrow(
      /startup adoption did not finish within 100 ms/,
    );
    expect(ensured).toBe(0);
    expect(cli.calls).toHaveLength(0);
  });

  it("fails closed when the host cannot say (the hook is not installed)", async () => {
    startupGate = Promise.reject(new Error("container-runner does not export startupAdoptionDone"));
    startupGate.catch(() => undefined);
    await expect(run()).rejects.toThrow(/does not export startupAdoptionDone/);
    expect(ensured).toBe(0);
  });
});

describe("the proxy URL's userinfo never leaves the module", () => {
  const URL_WITH_TOKEN = "http://agent:s3cr3t-agent-token@host.docker.internal:10255";

  it("scrubs URL userinfo and proxy variables, even from a docker argv in an error", () => {
    const text = safeText(
      `Command failed: docker create --name x -e HTTPS_PROXY=${URL_WITH_TOKEN} -e https_proxy=${URL_WITH_TOKEN} img; also ${URL_WITH_TOKEN}/path`,
      400,
    );
    expect(text).not.toContain("s3cr3t-agent-token");
    expect(text).not.toContain("agent:");
    expect(safeText("fetch failed https://user:pw@example.test:8443/x")).toBe(
      "fetch failed https://[redacted]@example.test:8443/x",
    );
  });

  it("rewrites startup and group lookup errors before they are thrown", async () => {
    const secret = "http://agent:tok@proxy.test:1";
    startupGate = Promise.reject(new Error(`adoption failed via ${secret}`));
    startupGate.catch(() => undefined);
    const err = await run().then(
      () => null,
      (e: Error) => e,
    );
    expect(err?.message).not.toContain("tok");
    expect(err?.message).toContain("[redacted]");
  });

  it("rewrites an error from the lease or the driver before it is thrown", async () => {
    gatewayEnv = { HTTPS_PROXY: URL_WITH_TOKEN };
    hangBefore.clear();
    const bad = probe();
    cli.responses = [
      {
        match: /^create /,
        throws: new Error(`Command failed: docker create -e HTTPS_PROXY=${URL_WITH_TOKEN} img`),
      },
    ];
    const error = await bad(GITHUB, [GITHUB], "github").then(
      () => null,
      (e: Error) => e,
    );
    expect(error?.message).toBeTruthy();
    expect(error?.message).not.toContain("s3cr3t-agent-token");
    expect(error?.message.length).toBeLessThanOrEqual(400);
  });
});

describe("the whole probe stays within its published bound", () => {
  it("ends within the deadline plus the bounded teardown steps when everything hangs", async () => {
    hangBefore.add("stop");
    hangBefore.add("release");
    child = () => "hang";
    const started = Date.now();
    await expect(run(probe({}, 120))).rejects.toThrow(/not torn down/);
    // deadline 120 + at most six teardown steps of 80 ms (the harness's bound) + slack
    expect(Date.now() - started).toBeLessThan(120 + 6 * 80 + 400);
  });
});

describe("the settle path and gateway availability fail closed", () => {
  it("a session that reads running again after `stopped` is not finished", async () => {
    child = () => "hang";
    statusScript = [{ phase: "stopped" }, { phase: "running" }];
    await expect(run(probe({}, 150))).rejects.toThrow(/did not finish within 150 ms/);
  });

  it("accepts only after `stopped` is confirmed, even through a running blip", async () => {
    child = () => ({ code: 0, verdict: verdict("github", GITHUB) });
    statusScript = [
      { phase: "running" },
      { phase: "stopped" },
      { phase: "running" },
      { phase: "stopped" },
      { phase: "stopped" },
    ];
    await expect(run(probe({}, 2000))).resolves.toBeDefined();
  });

  it("a gateway outage reported as the child ends is not a pass", async () => {
    unavailableAtExit = "OneCLI gateway unavailable";
    child = () => ({ code: 0, verdict: verdict("github", GITHUB) });
    await expect(run()).rejects.toThrow(/gateway became unavailable: OneCLI gateway unavailable/);
    expect(released).toBe(1);
    expect(removed()).toBe(true);
  });
});

describe("scratch left by a crashed host", () => {
  const OLD = new Date(Date.now() - 3_600_000);
  const id = (n: number) => `preflight-${n.toString(16).padStart(12, "0")}`;
  let parent: string;
  beforeEach(() => {
    parent = path.join(root, "data", "v2-sessions", "group-1");
    mkdirSync(parent, { recursive: true });
  });
  const dir = (name: string, old: boolean) => {
    const d = path.join(parent, name);
    mkdirSync(path.join(d, "preflight"), { recursive: true });
    if (old) utimesSync(d, OLD, OLD);
    return d;
  };

  it("removes only stale, unlisted, real preflight directories", async () => {
    const stale = dir(id(1), true);
    const fresh = dir(id(2), false);
    const listed = dir(id(3), true);
    const other = dir("sess-real-session", true);
    const odd = dir("preflight-xyz", true);
    const outside = path.join(root, "outside");
    mkdirSync(outside);
    writeFileSync(path.join(outside, "keep.txt"), "keep");
    symlinkSync(outside, path.join(parent, id(4)));
    utimesSync(path.join(parent, id(4)), OLD, OLD);
    writeFileSync(path.join(parent, id(5)), "a file");
    const removed = await sweepPreflightResidue(parent, new Set([id(3)]), 60_000);
    expect(removed).toEqual([id(1)]);
    expect(existsSync(stale)).toBe(false);
    for (const kept of [
      fresh,
      listed,
      other,
      odd,
      path.join(outside, "keep.txt"),
      path.join(parent, id(5)),
    ])
      expect(existsSync(kept)).toBe(true);
  });

  it("a probe sweeps it without touching another session or failing", async () => {
    const stale = dir(id(9), true);
    const other = dir("sess-live", true);
    await expect(run()).resolves.toBeDefined();
    expect(existsSync(stale)).toBe(false);
    expect(existsSync(other)).toBe(true);
  });

  it("a missing or unreadable parent is not an error", async () => {
    expect(await sweepPreflightResidue(path.join(root, "nope"), new Set(), 0)).toEqual([]);
  });
});

describe("the installed hook", () => {
  it("NanoClaw's container-runner exports ensureGatewaySession (install.sh patch, rebuilt dist)", async () => {
    const runner = await import("../container-runner.js");
    expect(typeof runner.ensureGatewaySession).toBe("function");
    const dist = path.join(process.cwd(), "dist", "container-runner.js");
    // A stale build would lack the export at runtime even though the source has it.
    if (existsSync(dist))
      expect(readFileSync(dist, "utf8")).toMatch(/export (async )?function ensureGatewaySession\(/);
  });
});

describe("one deadline across every operation, with cleanup", () => {
  it("times out a child that never exits, then stops and removes it", async () => {
    child = () => "hang";
    await expect(run(probe({}, 150))).rejects.toThrow(/did not finish within 150 ms/);
    expect(cli.joined().some((c) => /^stop /.test(c))).toBe(true);
    expect(removed()).toBe(true);
    expect(released).toBe(1);
  });

  for (const op of ["group", "ensure", "compose", "prepare", "start", "status"] as const) {
    it(`a hung ${op} fails by the deadline, leaves nothing behind and starts no container after`, async () => {
      hangBefore.add(op);
      const p = probe({}, 120);
      await expect(run(p)).rejects.toThrow(/did not finish within/);
      // The hung operation finishes late: whatever it produced must be cleaned up.
      hangBefore.delete(op);
      await release();
      expect(released).toBe(ensured);
      if (cli.callMatching(/^create /)) expect(removed()).toBe(true);
      if (op === "group" || op === "ensure" || op === "compose")
        expect(cli.callMatching(/^create /)).toBeUndefined();
      expect(cli.started.every((s) => s.proc.killed || !cli.callMatching(/^start /))).toBe(true);
    });
  }

  it("a prepare whose result arrives only after the deadline is stopped when it does arrive", async () => {
    hangAfter.add("prepare");
    await expect(run(probe({}, 120))).rejects.toThrow(/prepare did not finish within/);
    // `docker create` already ran; the handle was never returned in time.
    expect(cli.callMatching(/^create /)).toBeDefined();
    hangAfter.delete("prepare");
    await release();
    expect(removed()).toBe(true);
    expect(cli.started).toHaveLength(0);
  });

  it("a gateway lease that arrives after the deadline is released, not leaked", async () => {
    hangAfter.add("ensure");
    await expect(run(probe({}, 120))).rejects.toThrow(/gateway lease did not finish/);
    await release();
    expect(ensured).toBe(1);
    expect(released).toBe(1);
  });

  it("reports when a late gateway release fails, without leaking userinfo", async () => {
    releaseFails = true;
    hangAfter.add("ensure");
    const err = await (async () => {
      const started = Date.now();
      const finished = run(probe({}, 120)).then(
        () => null,
        (e: Error) => e,
      );
      while (ensured === 0) await sleep(1);
      await sleep(Math.max(1, 125 - (Date.now() - started)));
      await release();
      return finished;
    })();
    expect(err?.message).toMatch(/late cleanup:|not torn down \(late cleanup:/);
    expect(err?.message).not.toContain("tok");
    hangAfter.delete("ensure");
    await release();
  });

  it("fails closed when stop hangs: the container may still exist", async () => {
    hangBefore.add("stop");
    child = () => "hang";
    await expect(run(probe({}, 120))).rejects.toThrow(/not torn down \(stop: stop did not finish/);
    expect(released).toBe(1);
  });

  it("fails closed when the gateway release hangs", async () => {
    hangBefore.add("release");
    await expect(run()).rejects.toThrow(
      /not torn down \(gateway release: gateway release did not finish/,
    );
    expect(removed()).toBe(true);
  });

  it("fails closed when the container is still listed after stop", async () => {
    stillListed = true;
    await expect(run()).rejects.toThrow(/still listed after stop/);
  });

  it("a stop failure does not skip the gateway release", async () => {
    hangBefore.add("stop");
    await expect(run(probe({}, 120))).rejects.toThrow(/not torn down/);
    expect(released).toBe(1);
  });
});

describe("the composed spec is kept, never weakened", () => {
  it("keeps the composed security profile and refuses when the driver cannot realize it", async () => {
    profile = "codex-bwrap";
    const { assertDriverSupportsSecurityProfiles } = await import(
      "../drivers/argus-codex-sandbox.js"
    );
    const none = { isolationTiers: ["container"], securityProfiles: [] };
    await expect(
      run(
        probe({
          capabilities: none,
          assertSpecAllowed: (c, s) =>
            assertDriverSupportsSecurityProfiles(c as never, s, "docker"),
        }),
      ),
    ).rejects.toThrow(/security profile/i);
    expect(cli.callMatching(/^create /)).toBeUndefined();
    expect(prepared).toHaveLength(0);
    expect(released).toBe(1);
  });

  it("hands the driver the profile and the gateway's read-only mounts unchanged", async () => {
    profile = "codex-bwrap";
    const ca = path.join(root, "data", "gateway-trust", "ca.pem");
    mkdirSync(path.dirname(ca), { recursive: true });
    writeFileSync(ca, "synthetic ca");
    gatewayMounts = [
      {
        class: "gateway-trust",
        hostPath: ca,
        containerPath: "/etc/onecli/ca.pem",
        mode: "ro",
        groupScope: "group-1",
      },
    ];
    // The installed driver decides: it advertises the profile but refuses to run it unless the
    // daemon's AppArmor/seccomp options can be verified (the fake docker cannot show them).
    await expect(run()).rejects.toThrow(/could not read the Docker daemon security options/);
    expect(cli.callMatching(/^create /)).toBeUndefined();
    expect(released).toBe(1);
    expect(prepared[0].containers[0].securityProfile).toBe("codex-bwrap");
    expect(prepared[0].containers[0].mounts).toEqual(
      expect.arrayContaining([expect.objectContaining({ hostPath: ca, mode: "ro" })]),
    );
    const result = (
      prepared[0].containers[0].mounts as Array<{ containerPath: string; mode: string }>
    ).find((m) => m.containerPath === "/preflight");
    expect(result?.mode).toBe("rw");
  });

  it("makes the result directory writable for the container user, or refuses", async () => {
    const self = process.getuid?.() ?? 0;
    const ok = path.join(root, "ok");
    await prepareResultDir(ok, { uid: self, gid: process.getgid?.() ?? self });
    expect(statSync(ok).mode & 0o777).toBe(0o700);
    if (self !== 0)
      await expect(
        prepareResultDir(path.join(root, "other"), { uid: self + 1, gid: self }),
      ).rejects.toThrow(/cannot write the preflight result directory/);
  });
});

describe("the verdict file is read without trusting the container", () => {
  let dir: string;
  beforeEach(() => {
    dir = path.join(root, "r");
    mkdirSync(dir);
  });
  it("reads a plain verdict", async () => {
    writeFileSync(path.join(dir, "result.json"), '{"ok":true}');
    expect(await readVerdictFile(dir)).toEqual({ ok: true });
  });
  it("refuses a symlink to a file outside, a hard link, an oversized file and a non-object", async () => {
    const outside = path.join(root, "outside.json");
    writeFileSync(outside, '{"ok":true}');
    symlinkSync(outside, path.join(dir, "result.json"));
    expect(await readVerdictFile(dir)).toBeNull();
    rmSync(path.join(dir, "result.json"));
    linkSync(outside, path.join(dir, "result.json"));
    expect(await readVerdictFile(dir)).toBeNull();
    rmSync(path.join(dir, "result.json"));
    writeFileSync(path.join(dir, "result.json"), `{"ok":true,"pad":"${"x".repeat(70_000)}"}`);
    expect(await readVerdictFile(dir)).toBeNull();
    writeFileSync(path.join(dir, "result.json"), "[1]");
    expect(await readVerdictFile(dir)).toBeNull();
    writeFileSync(path.join(dir, "result.json"), "not json");
    expect(await readVerdictFile(dir)).toBeNull();
  });
  it("refuses a result directory that is itself a symlink, and a missing one", async () => {
    const real = path.join(root, "real");
    mkdirSync(real);
    writeFileSync(path.join(real, "result.json"), '{"ok":true}');
    symlinkSync(real, path.join(root, "link"));
    expect(await readVerdictFile(path.join(root, "link"))).toBeNull();
    expect(await readVerdictFile(path.join(root, "missing"))).toBeNull();
  });
});

describe("provider and allowlist mismatch", () => {
  const never = vi.fn(async () => {
    throw new Error("the runner must not start");
  });

  it("refuses a lease of the other provider or a repository outside the allowlist before any container", async () => {
    const guarded = sessionRepositoryProbe(never);
    await expect(guarded(GITHUB, [GITHUB], "gitea")).rejects.toThrow(
      /on github but the applied lease is gitea/,
    );
    await expect(guarded(GITEA, [GITEA], "github")).rejects.toThrow(
      /on gitea but the applied lease is github/,
    );
    await expect(guarded(GITHUB, ["https://github.com/acme/other"], "github")).rejects.toThrow(
      /allowlist/,
    );
    await expect(guarded(GITHUB, [GITEA], "github")).rejects.toThrow(/allowlist/);
    expect(never).not.toHaveBeenCalled();
  });
});

describe("the helper's verdict satisfies the host's contract (mocked forge: logic only)", () => {
  const exec = async (main: typeof githubMain, url: string, full: string, push: boolean) => {
    let out = "";
    const code = await main(
      ["repo-preflight", "--repository", url],
      { ARGUS_REPOSITORIES_JSON: JSON.stringify({ r: url }) },
      {
        fetch: async () => Response.json({ full_name: full, permissions: { pull: true, push } }),
        stdout: { write: (c: string) => (out += c) },
        stderr: { write() {} },
      },
    );
    return { exitCode: code, verdict: out ? JSON.parse(out) : null };
  };

  it("accepts a write-authenticated helper run and refuses a read-only one", async () => {
    const gh = await exec(githubMain, GITHUB, "acme/widget", true);
    expect(gh.exitCode).toBe(0);
    assertPreflightPositive(gh, { provider: "github", repository: GITHUB });
    const gt = await exec(giteaMain, GITEA, "acme/widget", true);
    assertPreflightPositive(gt, { provider: "gitea", repository: GITEA });
    expect(() =>
      assertPreflightPositive(
        { exitCode: 0, verdict: gh.verdict },
        { provider: "gitea", repository: GITEA },
      ),
    ).toThrow();
    expect((await exec(githubMain, GITHUB, "acme/widget", false)).exitCode).toBe(13);
  });
});

describe("outcome classification: denial, network, unproven and session failures differ", () => {
  const outcomeOf = (result: { exitCode: number; verdict: unknown }, access?: "read" | "write") => {
    try {
      assertPreflightPositive(result, { provider: "github", repository: GITHUB }, access);
      return "ok";
    } catch (error) {
      return error instanceof RepositoryPreflightError ? error.outcome : "untyped";
    }
  };
  const failed = (code: string, evidence: object) => ({
    exitCode: 13,
    verdict: { ok: false, evidence, error: { code, message: "x" } },
  });
  const publicNoBit = {
    http_status: 200,
    permissions: "present",
    push: false,
    visibility: "public",
  };

  it.each([
    ["401 from the forge", failed("unauthorized", { http_status: 401 }), "forge_denied"],
    ["403 from the forge", failed("forbidden", { http_status: 403 }), "forge_denied"],
    ["404 from the forge", failed("not_found", { http_status: 404 }), "forge_denied"],
    [
      "a private repository without push",
      failed("policy_denied", { ...publicNoBit, visibility: "private" }),
      "forge_denied",
    ],
    ["a public repository without push", failed("policy_denied", publicNoBit), "unknown"],
    [
      "a public repository without permissions",
      failed("policy_denied", { http_status: 200, permissions: "absent", visibility: "public" }),
      "unknown",
    ],
    [
      "an unlabelled repository without push",
      failed("policy_denied", { http_status: 200, permissions: "present", push: false }),
      "unknown",
    ],
    [
      "a helper that claims proof it did not show",
      failed("policy_denied", { http_status: 200, push: false, authentication: "proven" }),
      "unknown",
    ],
    ["a proxy or network error", failed("network_error", {}), "network_failure"],
    ["a rate limit", failed("rate_limited", { http_status: 429 }), "unknown"],
    ["a server error", failed("server_error", { http_status: 502 }), "unknown"],
    [
      "an old helper's verdict without evidence",
      { exitCode: 13, verdict: { ok: false, error: { code: "policy_denied" } } },
      "unknown",
    ],
    ["no verdict (killed)", { exitCode: 137, verdict: null }, "session_failure"],
    [
      "an ok verdict for write without push",
      { exitCode: 0, verdict: verdict("github", GITHUB, { authenticated: false, write: false }) },
      "unknown",
    ],
  ])("%s", (_label, result, outcome) => {
    expect(outcomeOf(result as { exitCode: number; verdict: unknown })).toBe(outcome);
  });

  it("read access on a public repository passes without claiming authentication", () => {
    const evidence = assertPreflightPositive(
      {
        exitCode: 0,
        verdict: verdict("github", GITHUB, {
          authenticated: false,
          write: false,
          evidence: { ...publicNoBit, authentication: "proven" },
        }),
      },
      { provider: "github", repository: GITHUB },
      "read",
    );
    expect(evidence).toMatchObject({
      visibility: "public",
      push: false,
      authentication: "unknown",
    });
  });

  it("a session that would run for another agent group is refused before any lease or container", async () => {
    const p = probe();
    await expect(p(GITHUB, [GITHUB], "github", "write", "ag-other")).rejects.toMatchObject({
      outcome: "assignment_missing",
    });
    expect(ensured).toBe(0);
    expect(cli.callMatching(/^create /)).toBeUndefined();
  });

  it("the session's own failures are session_failure", async () => {
    gatewayEnv = { SSL_CERT_FILE: "/tmp/onecli-combined-ca.pem" };
    await expect(run()).rejects.toMatchObject({ outcome: "session_failure" });
  });
});

describe("runRepositoryPreflight: lease and assignment first, then the probe, one event", () => {
  const LEASE = { lease_id: "lease-1", provider: "github", role: "repository" as const };
  const assigned = (over: Partial<RepositoryAssignmentCheck> = {}): RepositoryAssignmentCheck => ({
    agent: "found",
    secretMode: "selective",
    rest: { state: "assigned", scope: "match" },
    git: { state: "assigned", scope: "match" },
    scopes: {
      rest: { host: "api.github.com", path: "/repos/acme/widget/*" },
      git: { host: "github.com", path: "/acme/widget.git/*" },
    },
    ...over,
  });
  const preflight = (over: Partial<Parameters<typeof runRepositoryPreflight>[0]> = {}) => {
    const events: PreflightEvidenceEvent[] = [];
    const probeCalls = vi.fn(async () => ({
      helper_error: null,
      http_status: 200,
      permissions: "present" as const,
      push: true,
      visibility: "private" as const,
      authentication: "proven" as const,
    }));
    const done = runRepositoryPreflight({
      taskId: "task-1",
      attempt: 2,
      repositoryUrl: `${GITHUB}.git`,
      access: "write",
      lease: LEASE,
      verifyAssignment: async () => assigned(),
      probe: probeCalls,
      emit: (event) => {
        events.push(structuredClone(event));
      },
      ...over,
    });
    return { done, events, probeCalls };
  };

  it("passes with the assignment confirmed and the probe's evidence, attempt-correlated", async () => {
    const { done, events } = preflight();
    const result = await done;
    expect(result).toMatchObject({ outcome: "ok", reason: null, error: null });
    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      event: "repository_preflight",
      task_id: "task-1",
      attempt: 2,
      provider: "github",
      access: "write",
      outcome: "ok",
      lease: { source: "host", held: true, role: true, provider: true },
      assignment: { source: "onecli_admin_read", read: true, agent: "found" },
      target: {
        source: "host",
        api_host: "api.github.com",
        api_path: "/repos/acme/widget/*",
        git_host: "github.com",
        git_path: "/acme/widget.git/*",
      },
      probe: { source: "preflight_session_verdict", push: true, authentication: "proven" },
    });
    expect(events[0].preflight_id).toMatch(/^[0-9a-f]{16}$/);
  });

  it.each([
    ["no lease", { lease: null }],
    ["a lease of the other provider", { lease: { ...LEASE, provider: "gitea" } }],
    ["a lease of another role", { lease: { ...LEASE, role: "engine" as never } }],
  ])("%s is assignment_missing and never reads OneCLI or probes", async (_label, over) => {
    const verifyAssignment = vi.fn(async () => assigned());
    const { done, probeCalls } = preflight({ ...over, verifyAssignment });
    expect(await done).toMatchObject({
      outcome: "assignment_missing",
      reason: "env_repository_unavailable",
    });
    expect(verifyAssignment).not.toHaveBeenCalled();
    expect(probeCalls).not.toHaveBeenCalled();
  });

  it.each([
    ["no agent", { agent: "missing" as const }],
    [
      "the REST secret missing",
      { rest: { state: "missing" as const, scope: "unlisted" as const } },
    ],
    [
      "the Git secret unassigned",
      { git: { state: "unassigned" as const, scope: "match" as const } },
    ],
    [
      "a secret scoped elsewhere",
      { rest: { state: "assigned" as const, scope: "mismatch" as const } },
    ],
  ])(
    "%s is assignment_missing, distinct from a forge denial, with no probe",
    async (_label, over) => {
      const { done, probeCalls, events } = preflight({
        verifyAssignment: async () => assigned(over),
      });
      const result = await done;
      expect(result.outcome).toBe("assignment_missing");
      expect(result.reason).toBe("env_repository_unavailable");
      expect(result.error).toMatch(/^repository preflight assignment_missing: /);
      expect(probeCalls).not.toHaveBeenCalled();
      expect(events[0].probe).toBeNull();
    },
  );

  it("an unlisted scope is not a mismatch", async () => {
    const unlisted = { state: "assigned" as const, scope: "unlisted" as const };
    const { done } = preflight({
      verifyAssignment: async () => assigned({ rest: unlisted, git: unlisted }),
    });
    expect((await done).outcome).toBe("ok");
  });

  it("an unreadable OneCLI is assignment_unverified", async () => {
    const { done, probeCalls } = preflight({
      verifyAssignment: async () => {
        throw new Error("OneCLI GET /agents failed: 500");
      },
    });
    expect(await done).toMatchObject({
      outcome: "assignment_unverified",
      reason: "env_repository_unavailable",
    });
    expect(probeCalls).not.toHaveBeenCalled();
  });

  it.each([
    ["forge_denied", "env_repository_unreachable"],
    ["network_failure", "env_repository_unreachable"],
    ["unknown", "env_repository_unreachable"],
    ["session_failure", "env_repository_unreachable"],
  ] as const)("a probe outcome %s is reported as %s", async (outcome, reason) => {
    const { done, events } = preflight({
      probe: async () => {
        throw new RepositoryPreflightError(outcome, "the probe failed");
      },
    });
    expect(await done).toMatchObject({
      outcome,
      reason,
      error: `repository preflight ${outcome}: the probe failed`,
    });
    expect(events[0].outcome).toBe(outcome);
  });

  it("an untyped probe error is session_failure and a failing emit changes nothing", async () => {
    const { done } = preflight({
      probe: async () => {
        throw new Error("boom");
      },
      emit: () => {
        throw new Error("log post failed");
      },
    });
    expect(await done).toMatchObject({ outcome: "session_failure" });
  });

  it("no secret, auth header, proxy userinfo or sensitive URL reaches the event or the error", async () => {
    const TOKEN = "ghp_" + "Zx9".repeat(12);
    const PROXY_URL = "http://agent:s3cr3t-agent-token@host.docker.internal:10255";
    const leaky = `docker create -e HTTPS_PROXY=${PROXY_URL} Authorization: Bearer ${TOKEN} https://x-access-token:${TOKEN}@github.com/acme/widget.git`;
    const check = (text: string) => {
      expect(text).not.toContain(TOKEN);
      expect(text).not.toContain("s3cr3t-agent-token");
      expect(text).not.toContain("x-access-token:");
      expect(text).not.toMatch(/Bearer\s+\S{8,}/);
    };
    for (const probe of [
      async () => {
        throw new RepositoryPreflightError("network_failure", leaky);
      },
      async () => {
        throw new Error(leaky);
      },
    ]) {
      const { done, events } = preflight({
        taskId: `task-1 ${leaky}`,
        verifyAssignment: async () =>
          ({
            ...assigned(),
            secretMode: leaky,
            extra: leaky,
          }) as unknown as RepositoryAssignmentCheck,
        probe: probe as never,
      });
      const result = await done;
      check(result.error ?? "");
      check(JSON.stringify(events));
      expect(result.error?.length).toBeLessThanOrEqual(340);
      expect(JSON.stringify(events[0]).length).toBeLessThan(2000);
      expect(JSON.stringify(events[0])).not.toContain("extra");
    }
    const unreadable = preflight({
      verifyAssignment: async () => {
        throw new Error(leaky);
      },
    });
    check((await unreadable.done).error ?? "");
    check(JSON.stringify(unreadable.events));
  });
});
