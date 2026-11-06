/**
 * Single-layer experiment (docs/specs/single-layer-experiment.spec.md). Deterministic:
 * a fake Argus API and OneCLI gateway, no network, no model. A passing run here is not
 * an experiment result.
 */
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ChannelSetup } from "./adapter.js";
import { type ArgusTask, createArgusAdapter, type HeldAccounts, renderTask } from "./argus.js";
import { type AccountLease, OneCliClient } from "./argus-accounts.js";
import {
  armMarker,
  assignedArm,
  decideArm,
  harnessSelection,
  ineligibility,
  parseSingleLayerEnv,
  pullRequestsOf,
  SINGLE_LAYER_LINES,
} from "./argus-single-layer.js";
import { until } from "./argus-test-until.js";
import { policySnapshot, selectHarnessModel } from "./model-policy.mjs";

// Harness and implementation tiers differ here, so a test can see which one was taken.
const POLICY = {
  harness: { claude: ["claude-haiku-5-5"], codex: ["gpt-6-luna"] },
  implementation: { claude: ["claude-sonnet-5-5", "claude-opus-5-5"], codex: ["gpt-6.1-sol"] },
  hard_implementation: { claude: ["claude-opus-5-5"] },
};
const snapshot = () => policySnapshot(POLICY);

// NanoClaw's own database and container runtime are not under test.
vi.mock("../db/messaging-groups.js", () => ({
  getMessagingGroupByPlatform: async () => ({ id: "mg-1" }),
  getMessagingGroupAgents: async () => [{ agent_group_id: "group-1" }],
}));
vi.mock("../db/agent-groups.js", () => ({
  getAgentGroup: async (id: string) => ({ id, name: "argus worker", folder: "argus-worker" }),
}));
vi.mock("../db/sessions.js", () => ({
  getSessionsByAgentGroup: async () => [],
  findSessionForAgent: async () => undefined,
  updateSession: async () => undefined,
}));
vi.mock("./argus-local-forward.js", () => ({ syncLocalStrataForward: async () => undefined }));

const tmp: string[] = [];
afterEach(() => {
  for (const d of tmp.splice(0)) rmSync(d, { recursive: true, force: true });
});
const tempDir = () => {
  const d = mkdtempSync(path.join(os.tmpdir(), "argus-single-layer-"));
  tmp.push(d);
  return d;
};

describe("flag", () => {
  it.each([
    {},
    { ARGUS_SINGLE_LAYER: "" },
    { ARGUS_SINGLE_LAYER: "0" },
    { ARGUS_SINGLE_LAYER: "off" },
  ])("is off by default (%o)", (env) => {
    expect(parseSingleLayerEnv(env)).toEqual({ config: null, refused: null });
  });

  it.each(["codex", "cursor", "local", "custom", "copilot", "1"])(
    "refuses %s: only the Claude harness is supported",
    (engine) => {
      const r = parseSingleLayerEnv({
        ARGUS_SINGLE_LAYER: engine,
        ARGUS_SINGLE_LAYER_ASSIGNMENT: "/a",
      });
      expect(r.config).toBeNull();
      expect(r.refused).toBe(`unsupported_engine: ${engine} (only claude is supported)`);
    },
  );

  it("refuses claude without an assignment file", () => {
    expect(parseSingleLayerEnv({ ARGUS_SINGLE_LAYER: "claude" })).toEqual({
      config: null,
      refused: "no_assignment: ARGUS_SINGLE_LAYER_ASSIGNMENT unset",
    });
    expect(
      parseSingleLayerEnv({
        ARGUS_SINGLE_LAYER: "claude",
        ARGUS_SINGLE_LAYER_ASSIGNMENT: "/x.json",
      }).config,
    ).toEqual({ engine: "claude", assignmentFile: "/x.json" });
  });
});

describe("assignment", () => {
  const doc = {
    experiment: "p7-a",
    engine: "claude",
    arms: { t1: "single_layer", t2: "delegated" },
  };

  it("gives the listed arm and leaves unlisted tasks out of the experiment", () => {
    expect(assignedArm(doc, "t1")).toEqual({ experiment: "p7-a", arm: "single_layer" });
    expect(assignedArm(doc, "t2")).toEqual({ experiment: "p7-a", arm: "delegated" });
    expect(assignedArm(doc, "t3")).toBeNull();
  });

  it.each([
    [null, /not an object/],
    [{ ...doc, experiment: "bad id with spaces" }, /experiment id/],
    [{ ...doc, engine: "codex" }, /engine must be claude/],
    [{ ...doc, arms: undefined }, /no arms/],
    [{ ...doc, arms: { t1: "both" } }, /invalid/],
  ])("rejects a malformed file", (raw, error) => {
    expect(() => assignedArm(raw, "t1")).toThrow(error);
  });
});

describe("eligibility and the arm decision", () => {
  const claude = { engine: "claude", provider: "anthropic" };
  it.each([
    [{}, { engine: null, provider: null }, "no_engine_lease"],
    [{}, { engine: "codex", provider: "openai" }, "engine_not_claude"],
    [{}, { engine: "claude", provider: "local" }, "engine_not_claude"],
    [{}, { engine: "claude", provider: "custom" }, "engine_not_claude"],
    [{}, { engine: "agent", provider: "cursor" }, "engine_not_claude"],
    [{}, { engine: "copilot", provider: "github" }, "engine_not_claude"],
    [{ harness_model: { engine: "claude", model: "m" } }, claude, "harness_model_pinned"],
    [{ executor_model: { executor: "codex", model: "m" } }, claude, "executor_model_pinned"],
    [{ difficulty_executors: [] }, claude, "no_delegated_executor"],
    [{ difficulty_executors: ["claude"] }, claude, null],
    [{}, claude, null],
  ])("%o on %o: %s", (task, held, reason) => {
    expect(ineligibility(task, held)).toBe(reason);
  });

  it("falls back only from an assigned single_layer arm, keeping the assignment", () => {
    const sl = { experiment: "p7", arm: "single_layer" as const };
    const dl = { experiment: "p7", arm: "delegated" as const };
    expect(decideArm(null, "engine_not_claude")).toBeNull();
    expect(decideArm(sl, null)).toEqual({
      experiment: "p7",
      assigned: "single_layer",
      arm: "single_layer",
      fallback: null,
    });
    const fb = decideArm(sl, "engine_not_claude")!;
    expect(fb).toEqual({
      experiment: "p7",
      assigned: "single_layer",
      arm: "delegated",
      fallback: "engine_not_claude",
    });
    expect(armMarker(fb)).toBe(
      "single-layer experiment p7: arm=delegated assigned=single_layer fallback=engine_not_claude",
    );
    expect(armMarker(decideArm(dl, "engine_not_claude")!)).toBe(
      "single-layer experiment p7: arm=delegated",
    );
  });
});

describe("harness model", () => {
  const task = { spec: "Do it.", failures: 0, complexity: "standard" };

  it("with the flag off is exactly the default harness selection", () => {
    for (const t of [task, { ...task, complexity: "hard" }, { ...task, failures: 1 }])
      for (const engine of ["claude", "codex"])
        expect(harnessSelection(snapshot(), engine, t, "anthropic")).toEqual(
          selectHarnessModel(snapshot(), engine, t, "anthropic"),
        );
  });

  it("single-layer takes the implementation tier, with the retry position the harness would take", () => {
    expect(harnessSelection(snapshot(), "claude", task, "anthropic", true)).toMatchObject({
      scope: "implementation",
      configured_model: "claude-sonnet-5-5",
    });
    expect(
      harnessSelection(snapshot(), "claude", { ...task, failures: 1 }, "anthropic", true),
    ).toMatchObject({
      configured_model: "claude-opus-5-5",
      fallback_reason: "previous_task_failure",
    });
    expect(
      harnessSelection(snapshot(), "claude", { ...task, complexity: "hard" }, "anthropic", true),
    ).toMatchObject({ scope: "hard_implementation", configured_model: "claude-opus-5-5" });
  });

  it("keeps the harness selection for another engine or without a Claude implementation chain", () => {
    expect(harnessSelection(snapshot(), "codex", task, "openai", true).scope).toBe("harness");
    const bare = policySnapshot({ ...POLICY, implementation: { codex: ["gpt-6.1-sol"] } });
    expect(harnessSelection(bare, "claude", task, "anthropic", true).scope).toBe("harness");
  });
});

describe("prompt", () => {
  const base: ArgusTask = {
    id: "t1",
    title: "T",
    spec: "Do it.",
    project: "global",
    required_capabilities: [],
    attempt: 1,
    acceptance: [],
    knowledge_refs: [],
  } as unknown as ArgusTask;

  it("names no delegate on the single-layer arm and is unchanged without it", () => {
    const sl = renderTask(base, new Map(), "claude", { singleLayer: true });
    for (const line of SINGLE_LAYER_LINES) expect(sl).toContain(line);
    expect(sl).not.toContain("argus-delegate --");
    expect(sl).not.toContain("argus-model-policy.json");
    expect(renderTask(base, new Map(), "claude", { singleLayer: false })).toBe(
      renderTask(base, new Map(), "claude"),
    );
    expect(renderTask(base, new Map(), "claude")).not.toContain(SINGLE_LAYER_LINES[0]);
  });
});

describe("pullRequestsOf", () => {
  const repo = "https://git.example.test/org/repo.git";
  it("takes only pull requests of the task repository, once each", () => {
    expect(
      pullRequestsOf(
        repo,
        "Opened https://git.example.test/org/repo/pulls/7. Also (https://git.example.test/org/repo/pulls/7?x=1), " +
          "https://git.example.test/org/repo-fork/pulls/8 https://git.example.test/org/repo/issues/9 " +
          "https://evil.test/org/repo/pulls/10 https://git.example.test/org/repo/pull/11/",
      ),
    ).toEqual([
      { number: 7, url: "https://git.example.test/org/repo/pulls/7" },
      { number: 11, url: "https://git.example.test/org/repo/pull/11" },
    ]);
    expect(pullRequestsOf(undefined, "https://git.example.test/org/repo/pulls/7")).toEqual([]);
  });
  it("matches the owner and repository whatever their casing (2Mundos/argus #255)", () => {
    // The claim carries Argus's lowercase URL; the forge prints the owner's casing.
    expect(
      pullRequestsOf(
        "https://github.com/2mundos/argus",
        "Opened https://github.com/2Mundos/argus/pull/255 and https://github.com/2Mundos/argus-x/pull/3",
      ),
    ).toEqual([{ number: 255, url: "https://github.com/2Mundos/argus/pull/255" }]);
  });
});

// -- end to end: the adapter against a fake Argus API and OneCLI gateway --------------

type Call = { method: string; path: string; body: any };
const REPO_ID = "5e1f0c2a-7b3d-4e8f-9a6b-1c2d3e4f5a6b";
const REPOSITORY = "https://git.example.test/org/repo";
const PR = `${REPOSITORY}/pulls/7`;

const lease = (name: string, over: Partial<AccountLease> = {}): AccountLease => ({
  lease_id: `l-${name}`,
  account_id: `a-${name}`,
  name,
  provider: "anthropic",
  kind: "api_key",
  credential: { api_key: "dummy-key" },
  ...over,
});
const executorLease = () => lease("claude-2", { role: "executor" });
const repoLease = () =>
  lease("repo", {
    provider: "gitea",
    role: "repository",
    credential: { username: "service-user", token: "dummy-gitea-token" },
  });

const claim = (over: object = {}) => ({
  id: REPO_ID,
  title: "Fix the parser",
  spec: "Do it.",
  project: "global",
  required_capabilities: ["repository:write"],
  attempt: 1,
  acceptance: [],
  knowledge_refs: [],
  ...over,
});

/** Argus and OneCLI in one fetch. `progress` and `heartbeat` answers can change mid-run. */
function world(opts: { leases: AccountLease[]; task: object }) {
  const calls: Call[] = [];
  const leases = [...opts.leases];
  let task: object | undefined = opts.task;
  const state = { rejectHeartbeat: false, progressStatus: 200 };
  const agents: any[] = [];
  const secrets: any[] = [];
  const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status });
  const fetchImpl = (async (url: string, init: RequestInit) => {
    const u = new URL(url);
    // Artifact uploads are bytes, everything else JSON.
    const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
    const method = init.method ?? "GET";
    const p = u.pathname.replace(/^\/api/, "");
    if (u.hostname === "127.0.0.1") {
      if (method === "GET" && p === "/agents") return json(200, agents);
      if (method === "GET" && p === "/secrets") return json(200, secrets);
      if (method === "POST" && p === "/agents") {
        const a = { id: `ag-${agents.length}`, secretMode: "all", secrets: [], ...body };
        agents.push(a);
        return json(200, a);
      }
      if (method === "POST" && p === "/secrets") {
        const s = { id: `sec-${secrets.length}`, ...body };
        secrets.push(s);
        return json(200, s);
      }
      let m = /^\/agents\/([^/]+)\/secrets$/.exec(p);
      if (m && method === "GET")
        return json(200, agents.find((a) => a.id === m![1])?.secrets ?? []);
      if (m && method === "PUT") {
        agents.find((a) => a.id === m![1])!.secrets = body.secretIds;
        return json(200, { success: true });
      }
      m = /^\/agents\/([^/]+)\/secret-mode$/.exec(p);
      if (m && method === "PATCH") {
        agents.find((a) => a.id === m![1])!.secretMode = body.mode;
        return json(200, { success: true });
      }
      m = /^\/secrets\/([^/]+)$/.exec(p);
      if (m && method === "PATCH") {
        Object.assign(
          secrets.find((s) => s.id === m![1]),
          body,
        );
        return json(200, { success: true });
      }
      return json(404, { error: "unknown" });
    }
    calls.push({ method, path: p, body });
    if (p === "/agents/register")
      return json(201, { token: "t", agent: { id: "ag" }, heartbeat_interval_s: 1 });
    if (p === "/agents/me/heartbeat")
      return json(200, {
        state: "active",
        renewed: [],
        rejected: state.rejectHeartbeat
          ? [{ task_id: REPO_ID, attempt: 1, code: "not_claimed" }]
          : [],
      });
    if (p === "/accounts/lease") {
      const role = body?.role ?? "engine";
      const i = leases.findIndex((l) => (l.role ?? "engine") === role);
      if (i < 0) return json(409, { detail: { code: "no_account", message: "no usable account" } });
      return json(200, leases.splice(i, 1)[0]);
    }
    if (p === "/tasks/claim") {
      const t = task;
      task = undefined;
      if (t) return json(200, t);
      return new Promise<Response>((_, reject) =>
        init.signal?.addEventListener("abort", () =>
          reject(new DOMException("aborted", "AbortError")),
        ),
      );
    }
    if (p === `/tasks/${REPO_ID}/artifacts` && method === "POST")
      return json(201, { id: `art-${calls.length}` });
    if (p === `/tasks/${REPO_ID}/progress` && state.progressStatus !== 200)
      return json(state.progressStatus, {
        detail: { code: "stale_attempt", message: "the attempt is no longer current" },
      });
    return json(200, {});
  }) as unknown as typeof fetch;
  return { calls, fetchImpl, state };
}

const live: Array<{ teardown: () => Promise<void> }> = [];
afterEach(async () => {
  for (const a of live.splice(0)) await a.teardown();
});

/** One claim; `arms` is the assignment file, `undefined` turns the flag off. */
async function run(opts: {
  arms?: Record<string, string>;
  leases?: AccountLease[];
  task?: object;
  withPolicy?: boolean;
}) {
  const w = world({
    leases: opts.leases ?? [lease("claude-1"), executorLease(), repoLease()],
    task: opts.task ?? claim(),
  });
  let assignmentFile: string | undefined;
  if (opts.arms) {
    assignmentFile = path.join(tempDir(), "assignment.json");
    writeFileSync(
      assignmentFile,
      JSON.stringify({ experiment: "p7-test", engine: "claude", arms: opts.arms }),
    );
  }
  const selectModels = vi.fn(async (task: ArgusTask, held: HeldAccounts) =>
    harnessSelection(snapshot(), "claude", task as never, held.engine?.provider, held.single_layer),
  );
  const recycleRuntime = vi.fn(async () => undefined);
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
  const groupDir = tempDir();
  const adapter = createArgusAdapter({
    url: "http://argus.test",
    bootstrapToken: "boot",
    capabilities: [],
    name: "w",
    fetch: w.fetchImpl,
    claimWaitS: 1,
    heartbeatMs: 50,
    probeRepository: async () => {},
    workspaceRoot: async () => groupDir,
    // A claude lease must see the guard hook in the session's image, not on this host
    // (argus-guard.ts): the stub image carries the shipped managed settings.
    sessionImage: async () => "argus-nanoclaw-agent:test",
    readImageFile: async (image, file) => {
      expect([image, file]).toEqual([
        "argus-nanoclaw-agent:test",
        "/etc/claude-code/managed-settings.json",
      ]);
      return readFileSync(
        new URL("./engines/claude-managed-settings.json", import.meta.url),
        "utf8",
      );
    },
    ...(opts.withPolicy === false ? {} : { selectModels }),
    singleLayer: assignmentFile ? { engine: "claude", assignmentFile } : null,
    accounts: {
      models: true,
      onecli: new OneCliClient("http://127.0.0.1:10254", w.fetchImpl),
      resolveTarget: async () => ({ identifier: "ag-group-1", name: "Worker" }),
      repositories: { global: REPOSITORY },
      recycleRuntime,
    },
  });
  live.push(adapter);
  await adapter.setup(setup);
  await until(() => inbound.length > 0, 8000, "the task prompt");
  const say = (text: string) =>
    adapter.deliver("local", thread, { kind: "chat", content: { text } } as never);
  const posts = (suffix: string) => w.calls.filter((c) => c.path === `/tasks/${REPO_ID}${suffix}`);
  const harnessSel = () =>
    posts("/models")
      .flatMap((c) => c.body.selections ?? [])
      .find((s: any) => s.layer === "harness");
  return {
    w,
    prompt: inbound[0].content.text as string,
    selectModels,
    recycleRuntime,
    say,
    posts,
    harnessSel,
  };
}

describe("single-layer arm, end to end", () => {
  it("claims with the implementation tier, no delegate, an arm marker, and relays pr_opened", async () => {
    const r = await run({ arms: { [REPO_ID]: "single_layer" } });
    for (const line of SINGLE_LAYER_LINES) expect(r.prompt).toContain(line);
    expect(r.prompt).not.toContain("argus-delegate --");
    expect(r.selectModels.mock.calls[0]![1]).toMatchObject({ single_layer: true });
    expect(r.harnessSel()).toMatchObject({
      to_model: "claude-sonnet-5-5",
      explanation: "single-layer experiment p7-test: arm=single_layer",
    });

    await r.say(`PROGRESS: pushed and opened ${PR}`);
    await r.say(`COMPLETE: done, ${PR}`);
    await until(() => r.posts("/complete").length > 0, 8000, "complete");
    const prOpened = r.posts("/progress").filter((c) => c.body.kind === "pr_opened");
    expect(prOpened.map((c) => c.body)).toEqual([
      {
        attempt: 1,
        message: `Pull request opened: ${PR}`,
        kind: "pr_opened",
        data: { number: 7, url: PR },
      },
    ]);
  });

  it("a stale lease on pr_opened abandons the attempt and drops what follows", async () => {
    const r = await run({ arms: { [REPO_ID]: "single_layer" } });
    r.w.state.progressStatus = 409;
    await r.say(`PROGRESS: opened ${PR}`);
    await until(() => r.recycleRuntime.mock.calls.length > 0, 8000, "the session stops");
    await r.say("COMPLETE: done");
    await r.say("FAIL: broke");
    expect(r.posts("/complete")).toHaveLength(0);
    expect(r.posts("/fail")).toHaveLength(0);
  });

  it("a cancelled claim (heartbeat rejects the lease) stops the session and reports nothing more", async () => {
    const r = await run({ arms: { [REPO_ID]: "single_layer" } });
    r.w.state.rejectHeartbeat = true;
    await until(() => r.recycleRuntime.mock.calls.length > 0, 8000, "the session stops");
    await r.say(`COMPLETE: done, ${PR}`);
    expect(r.posts("/progress").filter((c) => c.body.kind === "pr_opened")).toHaveLength(0);
    expect(r.posts("/complete")).toHaveLength(0);
  });

  it("FAIL reports a failure under the attempt", async () => {
    const r = await run({ arms: { [REPO_ID]: "single_layer" } });
    await r.say("FAIL: the tests do not build");
    await until(() => r.posts("/fail").length > 0, 8000, "fail");
    expect(r.posts("/fail")[0]!.body).toMatchObject({ attempt: 1 });
  });
});

describe("default and fallback paths", () => {
  const view = (r: Awaited<ReturnType<typeof run>>) => ({
    prompt: r.prompt,
    held: r.selectModels.mock.calls.map((c) => c[1]),
    harness: (({ to_model, kind, explanation }) => ({ to_model, kind, explanation }))(
      r.harnessSel(),
    ),
  });

  it("flag off and unlisted tasks run exactly the default path, with no marker", async () => {
    const off = view(await run({}));
    await Promise.all(live.splice(0).map((a) => a.teardown()));
    const unlisted = view(await run({ arms: { "other-task": "single_layer" } }));
    expect(unlisted).toEqual(off);
    expect(off.held[0]).not.toHaveProperty("single_layer");
    expect(off.harness).toEqual({
      to_model: "claude-haiku-5-5",
      kind: "initial",
      explanation: undefined,
    });
    expect(off.prompt).not.toContain(SINGLE_LAYER_LINES[0]);
  });

  it("the delegated arm runs the default path and is marked", async () => {
    const off = view(await run({}));
    await Promise.all(live.splice(0).map((a) => a.teardown()));
    const r = await run({ arms: { [REPO_ID]: "delegated" } });
    const delegated = view(r);
    expect({ ...delegated, harness: { ...delegated.harness, explanation: undefined } }).toEqual(
      off,
    );
    expect(delegated.harness.explanation).toBe("single-layer experiment p7-test: arm=delegated");

    await r.say(`PROGRESS: opened ${PR}`);
    await until(
      () => r.posts("/progress").some((c) => c.body.kind === "pr_opened"),
      8000,
      "pr_opened",
    );
    const prOpened = r.posts("/progress").filter((c) => c.body.kind === "pr_opened");
    expect(prOpened.map((c) => c.body)).toEqual([
      {
        attempt: 1,
        message: `Pull request opened: ${PR}`,
        kind: "pr_opened",
        data: { number: 7, url: PR },
      },
    ]);
  });

  it.each<[Parameters<typeof run>[0], string]>([
    [
      { task: claim({ executor_model: { executor: "claude", model: "claude-opus-5-5" } }) },
      "executor_model_pinned",
    ],
    [{ withPolicy: false }, "no_model_policy"],
  ])(
    "an ineligible single_layer claim falls back to the default path with the reason",
    async (o, reason) => {
      const r = await run({ arms: { [REPO_ID]: "single_layer" }, ...o });
      expect(r.prompt).not.toContain(SINGLE_LAYER_LINES[0]);
      expect(r.prompt).toContain("argus-delegate");
      if (o.withPolicy !== false)
        expect(r.selectModels.mock.calls[0]![1]).not.toHaveProperty("single_layer");
      expect(r.harnessSel().explanation).toBe(
        `single-layer experiment p7-test: arm=delegated assigned=single_layer fallback=${reason}`,
      );
    },
  );

  it("a Codex engine lease falls back with engine_not_claude", async () => {
    const codex = lease("codex-1", {
      provider: "openai",
      kind: "subscription",
      credential: {
        auth_mode: "chatgpt",
        OPENAI_API_KEY: null,
        tokens: {
          id_token: "id-x",
          access_token: "access-x",
          refresh_token: "refresh-x",
          account_id: "acct",
        },
        last_refresh: "2026-01-01T00:00:00Z",
      },
    });
    const r = await run({
      arms: { [REPO_ID]: "single_layer" },
      leases: [codex, executorLease(), repoLease()],
    });
    expect(r.prompt).not.toContain(SINGLE_LAYER_LINES[0]);
    expect(r.harnessSel().explanation).toBe(
      "single-layer experiment p7-test: arm=delegated assigned=single_layer fallback=engine_not_claude",
    );
  });
});
