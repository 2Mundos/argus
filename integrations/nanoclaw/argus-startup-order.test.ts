/**
 * The preflight session against NanoClaw's REAL startup adoption (container-runner.ts as
 * patched by patch-startup-adoption-gate.py), real DB and a fake driver.
 *
 * index.ts starts the channel adapters (the Argus claim loop) BEFORE it calls
 * adoptRunningSessions(), which stops every listed session without a database row; a
 * preflight session has none. This pins both halves: the hazard (adoption does stop such a
 * session) and the gate (nothing is created before adoption has settled), with a slow
 * gateway lease racing the adoption.
 */
import fs from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SupervisedHandle, SupervisedSnapshot } from "../drivers/session-events.js";

const { snapshots, listSessions } = vi.hoisted(() => {
  const snapshots: unknown[] = [];
  return { snapshots, listSessions: vi.fn(async () => snapshots) };
});
vi.mock("../config.js", async () => {
  const actual = await vi.importActual<typeof import("../config.js")>("../config.js");
  const root = "/tmp/nanoclaw-test-argus-startup-order";
  return { ...actual, DATA_DIR: `${root}/data`, GROUPS_DIR: `${root}/groups` };
});
vi.mock("../drivers/index.js", () => {
  const driver = { kind: "fake", listSessions, prepare: vi.fn(), capabilities: () => ({}) };
  return { getSessionDriver: () => driver, isSessionEventsDriver: () => false };
});
vi.mock("../log.js", () => ({
  log: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn(), fatal: vi.fn() },
}));
vi.mock("../gateway-providers/index.js", () => ({
  getGatewayProvider: () => ({ sessions: { reapOrphans: async () => undefined } }),
  resetGatewayProvider: vi.fn(),
}));

// `startupAdoptionDone` settles once per process: every test gets a fresh module graph, as a
// freshly started host has.
let mods: Awaited<ReturnType<typeof load>>;
async function load() {
  vi.resetModules();
  const runner = await import("../container-runner.js");
  const db = await import("../db/index.js");
  const preflight = await import("./argus-repo-preflight.js");
  return { ...runner, ...db, ...preflight };
}

function preflightHandle(sessionId: string) {
  const stop = vi.fn(async (_reason: string) => undefined);
  const handle = {
    key: { installSlug: "slug", agentGroupId: "group-1", sessionId },
    name: `nanoclaw-v2-${sessionId}`,
    start: async () => undefined,
    stop,
    status: async () => ({ phase: "running" }),
    onTerminal: () => undefined,
  } as unknown as SupervisedHandle;
  return { handle, stop };
}

beforeEach(async () => {
  snapshots.length = 0;
  listSessions.mockClear();
  mods = await load();
  await mods.runMigrations(await mods.initTestDb());
});
afterEach(async () => {
  await mods.closeDb();
  fs.rmSync("/tmp/nanoclaw-test-argus-startup-order", { recursive: true, force: true });
});

describe("startup order", () => {
  it("index.ts still starts the channels before it adopts sessions (why the gate exists)", () => {
    const index = fs.readFileSync(path.join(process.cwd(), "src", "index.ts"), "utf8");
    const channels = index.indexOf("initChannelAdapters(");
    const adoption = index.indexOf("await adoptRunningSessions()");
    expect(channels).toBeGreaterThan(-1);
    expect(adoption).toBeGreaterThan(channels);
  });

  it("when adoption cannot list sessions, adoption throws and the gate rejects readiness", async () => {
    listSessions.mockImplementationOnce(async () => {
      throw new Error("driver list failed");
    });
    await expect(mods.adoptRunningSessions()).rejects.toThrow("driver list failed");
    await expect(mods.startupAdoptionDone).rejects.toThrow("driver list failed");
    let ready = false;
    let blocked = false;
    void mods.nanoclawStartupReady().then(
      () => {
        ready = true;
      },
      () => {
        blocked = true;
      },
    );
    await vi.waitFor(() => expect(blocked).toBe(true));
    expect(ready).toBe(false);
  });

  it("adoption stops a listed session that has no database row: a preflight started early would die", async () => {
    const early = preflightHandle("preflight-aaaaaaaaaaaa");
    snapshots.push({ handle: early.handle, phase: "running" } as unknown as SupervisedSnapshot);
    await mods.adoptRunningSessions();
    expect(early.stop).toHaveBeenCalledWith("orphan-at-startup");
  });

  it("the gate settles only when adoption returns, and stays shut while listing is slow", async () => {
    let finishListing!: () => void;
    listSessions.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishListing = () => resolve(snapshots);
        }) as never,
    );
    let settled = false;
    void mods.nanoclawStartupReady().then(() => {
      settled = true;
    });
    const adopting = mods.adoptRunningSessions();
    await new Promise((r) => setTimeout(r, 50));
    expect(settled).toBe(false);
    finishListing();
    await adopting;
    await mods.startupAdoptionDone;
    await vi.waitFor(() => expect(settled).toBe(true));
  });

  it("a preflight racing a slow adoption creates nothing until adoption is done, and then survives it", async () => {
    let finishListing!: () => void;
    listSessions.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishListing = () => resolve(snapshots);
        }) as never,
    );
    const ensured = vi.fn(async () => {
      // A slow gateway lease, as the real OneCLI call can be.
      await new Promise((r) => setTimeout(r, 40));
      return {
        lease: { contribution: { env: { HTTPS_PROXY: "http://proxy.test:1" } } },
        controller: new AbortController(),
      };
    });
    const created = preflightHandle("preflight-bbbbbbbbbbbb");
    const run = mods.ephemeralSessionRunner({
      installSlug: "slug",
      startupReady: mods.nanoclawStartupReady,
      group: async () => ({ id: "group-1", name: "Argus" }),
      sessionDir: (g, s) => path.join("/tmp/nanoclaw-test-argus-startup-order", g, s),
      driver: () =>
        ({
          capabilities: () => ({}),
          prepare: async () => ({
            start: async () => undefined,
            status: async () => ({ phase: "stopped" }),
            stop: created.stop,
          }),
          // Once adoption's own listing is done, a created preflight is listed until stopped.
          listSessions: async () => [],
        }) as never,
      ensureGateway: ensured as never,
      releaseGateway: async () => undefined,
      compose: async () =>
        ({
          key: { installSlug: "slug", agentGroupId: "group-1", sessionId: "x" },
          labels: {},
          containers: [{ role: "agent", env: {}, mounts: [] }],
          runAs: { uid: process.getuid?.() ?? 1000, gid: process.getgid?.() ?? 1000 },
        }) as never,
      pollMs: 5,
      teardownStepMs: 200,
    });
    const probing = run({
      helper: "argus-github-api",
      repositoryUrl: "https://github.com/a/b.git",
      timeoutMs: 5000,
    });
    probing.catch(() => undefined);
    const adopting = mods.adoptRunningSessions();
    await new Promise((r) => setTimeout(r, 120));
    expect(ensured).not.toHaveBeenCalled(); // gated: no lease while adoption lists sessions
    finishListing();
    await adopting;
    await probing.catch(() => undefined); // verdict absent in this fake: it fails closed, after running
    expect(ensured).toHaveBeenCalledTimes(1);
    // Adoption was over before the preflight existed, so it was never a candidate to stop.
    expect(created.stop).not.toHaveBeenCalledWith("orphan-at-startup");
  });
});
