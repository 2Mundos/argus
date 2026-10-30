// Installed-runtime test of unknown steer outcomes (docs/specs/task-steering.spec.md
// 3.3-3.6): the real channel against a real Argus API on a disposable database. A
// heartbeat carries a steer, the channel injects it and its ack never reaches Argus, the
// attempt is released, and the next worker's prompt warns of one unconfirmed steer
// without its text. Not in make check, pnpm test, or CI (same convention as
// argus-task-inputs.e2e.test.ts). Run manually via scripts/steer-unknown-e2e.sh, which
// starts the API, creates the task and passes everything in ARGUS_E2E_*; skipped when
// ARGUS_E2E_API_URL is unset. API bodies are `any`: the assertions check their shape.
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterAll, describe, expect, it, vi } from "vitest";
import type { ChannelSetup } from "./adapter.js";
import { createArgusAdapter } from "./argus.js";

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

const evidence: Record<"API" | "installed-runtime", string[]> = {
  API: [],
  "installed-runtime": [],
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
  const steers: any[] = [];
  const setup = {
    onInbound: vi.fn(async (_p: string, _t: string | null, m: any) => {
      if (String(m.id).startsWith("argus-steer-")) {
        // Injected, then the agent hangs before the ack: the steer was carried and
        // may have been applied, and Argus never hears so.
        steers.push(m);
        await new Promise(() => {});
      }
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
    capabilities: ["steer-e2e"],
    name,
    claimWaitS: 1,
    probeRepository: async () => {},
    workspaceRoot: async () => groupDir,
    accounts: undefined,
  });
  return { adapter, setup, inbound, steers };
}

const HEARTBEAT_MS = 45_000; // the API's 30 s interval, with slack

describe.skipIf(!API)("unknown steers, installed runtime", () => {
  it("a carried, unacknowledged steer ends unknown and the next attempt is warned by count", async () => {
    const admin = need("ARGUS_E2E_ADMIN_TOKEN");
    const taskId = need("ARGUS_E2E_TASK_ID");
    const text = `e2e direction ${Date.now()}: switch to the slower algorithm`;

    // Worker A claims attempt 1; its heartbeat advertises the steer feature.
    const a = worker("e2e-a", need("ARGUS_E2E_BOOTSTRAP_A"));
    await a.adapter.setup(a.setup);
    await until(() => a.inbound.length > 0, 30_000, "worker A's prompt");
    expect(a.inbound[0].content.text).not.toContain("Operator steers:");
    await until(
      async () =>
        ((await api("GET", "/agents", admin)).data as any[]).some(
          (g) => g.name === "e2e-a" && (g.runtime?.features ?? []).includes("steer"),
        ),
      HEARTBEAT_MS,
      "a heartbeat with the steer feature",
    );
    const made = await api("POST", `/tasks/${taskId}/steers`, admin, { message: text });
    expect(made.status, JSON.stringify(made.data)).toBe(201);
    const steerId = made.data.id as string;
    note("API", `steer ${steerId} created on attempt ${made.data.attempt}: pending`);

    // The next heartbeat carries it: dispatch is on record; the injection never acks.
    const listed = async () => (await api("GET", `/tasks/${taskId}/steers?attempt=1`, admin)).data;
    await until(() => a.steers.length > 0, HEARTBEAT_MS, "the steer injection");
    expect(a.steers[0].id).toBe(`argus-steer-${steerId}`);
    expect(a.steers[0].content.text).toContain(text);
    let row = (await listed()).items[0];
    expect(row.state).toBe("pending");
    expect(row.dispatched_at).toBeTruthy();
    expect(row.delivered_at).toBeNull();
    note("installed-runtime", `worker A: heartbeat carried the steer, injected, no ack sent`);
    note("API", `after dispatch: state=${row.state}, dispatched_at=${row.dispatched_at}`);

    const released = await api("POST", `/tasks/${taskId}/release`, admin, {
      reason: "e2e: the worker went quiet",
      attempt: 1,
    });
    expect(released.status).toBe(200);
    await a.adapter.teardown();
    row = (await listed()).items[0];
    expect(row.state).toBe("unknown");
    expect(row.closed_at).toBeTruthy();
    note("API", `after release: state=${row.state}, closed_at=${row.closed_at}`);

    // Worker B claims attempt 2: warned by count, never handed the text again.
    const b = worker("e2e-b", need("ARGUS_E2E_BOOTSTRAP_B"));
    try {
      await b.adapter.setup(b.setup);
      await until(() => b.inbound.length > 0, 30_000, "worker B's prompt");
      expect((await api("GET", `/tasks/${taskId}`, admin)).data.attempt).toBe(2);
      const prompt = b.inbound[0].content.text as string;
      const warning = prompt.split("\n").find((l) => l.startsWith("Operator steers:"));
      expect(warning).toContain("1 steer(s) sent to the previous attempt were never confirmed");
      expect(prompt).not.toContain(text);
      note("installed-runtime", `worker B, attempt 2 prompt: ${warning}`);

      // A full heartbeat later, nothing was reinjected and the outcome stands.
      const beats = (await api("GET", "/agents", admin)).data.find((g: any) => g.name === "e2e-b");
      const seen = beats?.last_heartbeat_at;
      await until(
        async () =>
          ((await api("GET", "/agents", admin)).data as any[]).some(
            (g) => g.name === "e2e-b" && g.last_heartbeat_at && g.last_heartbeat_at !== seen,
          ),
        HEARTBEAT_MS,
        "a heartbeat from worker B",
      );
      expect(b.steers).toEqual([]);
      expect(b.inbound.some((m) => String(m.id).startsWith("argus-steer-"))).toBe(false);
      expect((await listed()).items.map((s: any) => s.state)).toEqual(["unknown"]);
      const attempts = (await api("GET", `/tasks/${taskId}/attempts`, admin)).data as any[];
      expect(attempts.map((t) => t.steer_unknown)).toEqual([1, 0]);
      note("installed-runtime", "worker B: a heartbeat later, no steer injected");
      note(
        "API",
        `attempts steer_unknown: ${attempts.map((t) => `${t.attempt}:${t.steer_unknown}`).join(", ")}`,
      );
    } finally {
      await b.adapter.teardown();
    }
  }, 240_000);
});
