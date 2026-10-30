// Installed-runtime test of task inputs (docs/specs/task-inputs.spec.md, verification
// boundary): the real channel against a real Argus API on a disposable database. Run by
// scripts/task-input-e2e.sh, which starts the API, creates the task, uploads the input and
// passes everything in ARGUS_E2E_*; skipped otherwise. No model and no session container
// run: the channel materializes before either would start. API bodies are `any`: the
// assertions check their shape.
import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmodSync, cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, describe, expect, it, vi } from "vitest";
import type { ChannelSetup } from "./adapter.js";
import { createArgusAdapter } from "./argus.js";
import { INPUT_CONSUMED, INPUT_MANIFEST, INPUT_VERIFIER, TASK_INPUTS_DIR } from "./argus-inputs.js";

// NanoClaw's own database and container runtime are not part of this test; the channel is.
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
vi.mock("../container-runner.js", () => ({
  isContainerRunning: () => false,
  killContainer: () => undefined,
}));

const env = process.env;
const API = env.ARGUS_E2E_API_URL ?? "";
const need = (name: string): string => {
  const v = env[name];
  if (!v) throw new Error(`${name} is not set`);
  return v;
};

const evidence: Record<"API" | "installed-runtime" | "consumer", string[]> = {
  API: [],
  "installed-runtime": [],
  consumer: [],
};
const note = (section: keyof typeof evidence, line: string) => evidence[section].push(line);

const cleanup: string[] = [];
afterAll(() => {
  for (const d of cleanup.splice(0)) {
    spawnSync("chmod", ["-R", "u+w", d]);
    rmSync(d, { recursive: true, force: true });
  }
  if (!API) return;
  const out = Object.entries(evidence)
    .map(([k, lines]) => [`== evidence: ${k} ==`, ...lines].join("\n"))
    .join("\n");
  // The vitest console is quiet in this checkout; the script prints this file.
  if (env.ARGUS_E2E_EVIDENCE) writeFileSync(env.ARGUS_E2E_EVIDENCE, `${out}\n`);
  else process.stdout.write(`${out}\n`);
});

async function api(
  method: string,
  p: string,
  token: string,
  body?: unknown,
): Promise<{ status: number; data: any }> {
  const res = await fetch(`${API}/api${p}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  let data: any = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  return { status: res.status, data };
}

const until = async (cond: () => boolean | Promise<boolean>, ms: number, what: string) => {
  const start = Date.now();
  while (!(await cond())) {
    if (Date.now() - start > ms) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 100));
  }
};

function worker(name: string, bootstrapToken: string) {
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
  const groupDir = mkdtempSync(path.join(os.tmpdir(), `argus-e2e-${name}-`));
  cleanup.push(groupDir);
  const adapter = createArgusAdapter({
    url: API,
    bootstrapToken,
    capabilities: ["task-inputs"],
    name,
    claimWaitS: 1,
    probeRepository: async () => {},
    workspaceRoot: async () => groupDir,
    accounts: undefined,
  });
  return { adapter, setup, inbound, groupDir, thread: () => thread };
}

describe.skipIf(!API)("task inputs, installed runtime", () => {
  it("a second worker on attempt 2 materializes the same bytes and applies the patch", async () => {
    const admin = need("ARGUS_E2E_ADMIN_TOKEN");
    const taskId = need("ARGUS_E2E_TASK_ID");
    const artifactId = need("ARGUS_E2E_ARTIFACT_ID");
    const unsharedId = need("ARGUS_E2E_UNSHARED_ID");
    const fixture = readFileSync(need("ARGUS_E2E_INPUT_FILE"));
    const baseRepo = need("ARGUS_E2E_BASE_REPO");
    const sha256 = createHash("sha256").update(fixture).digest("hex");
    expect(sha256).toBe(need("ARGUS_E2E_INPUT_SHA256"));
    const inputName = need("ARGUS_E2E_INPUT_NAME");

    const listing = await api("GET", `/tasks/${taskId}/inputs`, admin);
    expect(listing.status).toBe(200);
    const items = listing.data.inputs as any[];
    expect(items.map((i) => i.id)).toEqual([artifactId]);
    expect(items.map((i) => i.id)).not.toContain(unsharedId);
    note(
      "API",
      `inputs view: ${items.length} shared (${artifactId}, ${items[0].size} bytes, sha256 ${items[0].sha256}); unshared_attachments=${listing.data.unshared_attachments ?? "?"}`,
    );
    const cont = await api("GET", `/tasks/${taskId}/continuation`, admin);
    expect(cont.data.wip?.ref).toBe(`attachment:${artifactId}:${sha256}`);
    note("API", `continuation wip.ref: ${cont.data.wip.ref}`);

    // Worker A: claims attempt 1 and materializes; the orchestrator then releases it.
    const a = worker("e2e-a", need("ARGUS_E2E_BOOTSTRAP_A"));
    await a.adapter.setup(a.setup);
    await until(() => a.inbound.length > 0, 30_000, "worker A's prompt");
    const aFile = path.join(a.groupDir, TASK_INPUTS_DIR, taskId, inputName);
    expect(readFileSync(aFile).equals(fixture)).toBe(true);
    const released = await api("POST", `/tasks/${taskId}/release`, admin, {
      reason: "e2e: hand the task to another worker",
      attempt: 1,
    });
    expect(released.status).toBe(200);
    await a.adapter.teardown();
    note("installed-runtime", `worker A: attempt 1 materialized ${fixture.length} bytes, released`);

    // Worker B: another registration and another group dir, attempt 2.
    const b = worker("e2e-b", need("ARGUS_E2E_BOOTSTRAP_B"));
    expect(b.groupDir).not.toBe(a.groupDir);
    try {
      await b.adapter.setup(b.setup);
      await until(() => b.inbound.length > 0, 30_000, "worker B's prompt");
      const task = await api("GET", `/tasks/${taskId}`, admin);
      expect(task.data.attempt).toBe(2);
      const dir = path.join(b.groupDir, TASK_INPUTS_DIR, taskId);
      const file = path.join(dir, inputName);
      const bytes = readFileSync(file);
      expect(bytes.equals(fixture)).toBe(true);
      expect(createHash("sha256").update(bytes).digest("hex")).toBe(sha256);
      const manifest = JSON.parse(readFileSync(path.join(dir, INPUT_MANIFEST), "utf8"));
      expect(manifest.inputs.map((i: any) => i.artifact_id)).toEqual([artifactId]);
      const prompt = b.inbound[0].content.text as string;
      expect(prompt).toContain(
        `/workspace/agent/${TASK_INPUTS_DIR}/${taskId}/${inputName} · ${fixture.length} bytes · sha256 ${sha256} · required`,
      );
      expect(prompt).not.toContain(API);
      expect(prompt).not.toContain(fixture.subarray(0, 200).toString("utf8"));
      note(
        "installed-runtime",
        `worker B: attempt 2, ${b.groupDir} (not A's), ${bytes.length} bytes, sha256 ${sha256}`,
      );
      note(
        "installed-runtime",
        `prompt line: ${prompt.split("\n").find((l) => l.includes(sha256))}`,
      );

      // The consumer: the verifier the channel wrote, then the patch on the exact base.
      const verify = spawnSync("node", [INPUT_VERIFIER], { cwd: dir, encoding: "utf8" });
      expect(verify.status).toBe(0);
      expect(verify.stdout).toContain(`${inputName} ${fixture.length} ${sha256} ok`);
      note("consumer", `node ${INPUT_VERIFIER}: exit ${verify.status}: ${verify.stdout.trim()}`);
      const clone = mkdtempSync(path.join(os.tmpdir(), "argus-e2e-clone-"));
      cleanup.push(clone);
      execFileSync("git", ["clone", "-q", baseRepo, clone]);
      const head = execFileSync("git", ["-C", clone, "rev-parse", "HEAD"], { encoding: "utf8" });
      const check = spawnSync("git", ["-C", clone, "apply", "--check", file], { encoding: "utf8" });
      expect(check.status, check.stderr).toBe(0);
      note("consumer", `git apply --check on ${head.trim().slice(0, 12)} (fresh clone): exit 0`);

      // Heartbeats of the holder advertise the feature (spec 3.8).
      await until(
        async () => {
          const agents = (await api("GET", "/agents", admin)).data as any[];
          return agents.some(
            (g) => g.name === "e2e-b" && (g.runtime?.features ?? []).includes("task-inputs"),
          );
        },
        45_000,
        "a heartbeat with the task-inputs feature",
      );
      note("API", "GET /api/agents: e2e-b runtime.features includes task-inputs");

      // The attempt ends: the channel relays `consumed` from the verifier's file.
      await b.adapter.deliver("local", b.thread(), {
        kind: "chat",
        content: "COMPLETE: verified the input and checked the patch",
      });
      await until(
        async () =>
          (
            (await api("GET", `/tasks/${taskId}/inputs`, admin)).data.inputs[0].receipts as any[]
          ).some((r) => r.state === "consumed"),
        15_000,
        "the consumed receipt",
      );
      const receipts = (await api("GET", `/tasks/${taskId}/inputs`, admin)).data.inputs[0]
        .receipts as any[];
      const states = receipts.map((r) => `${r.attempt}:${r.state}`);
      expect(states).toEqual(
        expect.arrayContaining(["1:materialized", "2:materialized", "2:verified", "2:consumed"]),
      );
      note("API", `receipts: ${states.join(", ")}`);
      expect(JSON.parse(readFileSync(path.join(dir, INPUT_CONSUMED), "utf8"))).toBeTruthy();

      // Negative: a corrupted local copy fails the verifier.
      const copy = mkdtempSync(path.join(os.tmpdir(), "argus-e2e-corrupt-"));
      cleanup.push(copy);
      cpSync(dir, copy, { recursive: true });
      const target = path.join(copy, inputName);
      chmodSync(target, 0o600);
      const tampered = Buffer.from(fixture);
      tampered[tampered.length - 2] ^= 1;
      writeFileSync(target, tampered);
      const bad = spawnSync("node", [INPUT_VERIFIER], { cwd: copy, encoding: "utf8" });
      expect(bad.status).not.toBe(0);
      expect(bad.stdout).toContain("FAIL");
      note("consumer", `corrupted copy: exit ${bad.status}: ${bad.stdout.trim()}`);
    } finally {
      await b.adapter.teardown();
    }
  }, 180_000);

  it("another task's worker cannot read the input, nor an unshared attachment", async () => {
    const artifactId = need("ARGUS_E2E_ARTIFACT_ID");
    const unsharedId = need("ARGUS_E2E_UNSHARED_ID");
    const taskId = need("ARGUS_E2E_TASK_ID");
    const otherTask = need("ARGUS_E2E_OTHER_TASK_ID");
    const reg = await fetch(`${API}/api/agents/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bootstrap_token: need("ARGUS_E2E_BOOTSTRAP_C"), name: "e2e-c" }),
    });
    expect(reg.status).toBe(201);
    const token = ((await reg.json()) as { token: string }).token;
    const claim = await api("POST", "/tasks/claim", token, {
      capabilities: ["e2e-other"],
      wait_s: 0,
    });
    expect(claim.status).toBe(200);
    expect(claim.data.id).toBe(otherTask);
    for (const [label, p] of [
      ["the shared input of another task", `/artifacts/${artifactId}/content`],
      ["the other task's inputs view", `/tasks/${taskId}/inputs`],
      ["an unshared attachment", `/artifacts/${unsharedId}/content`],
    ] as const) {
      const res = await api("GET", p, token);
      expect(res.status, label).toBe(403);
      note(
        "API",
        `worker of ${otherTask.slice(0, 8)}: ${label}: ${res.status} ${res.data?.detail?.code ?? ""}`.trim(),
      );
    }
  });
});
