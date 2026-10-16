import { describe, expect, it } from "vitest";
import type { Instance, Pool, PoolCapability } from "./api";
import { launchGate, launchStatus } from "./launch";

const pool = { name: "web", desired_replicas: 1, status: "ok", last_error: null } as Pool;
const cap = (over: Partial<PoolCapability> = {}) =>
  ({ launchable: true, free_slots: 1, blockers: [], ...over }) as PoolCapability;
const inst = (over: Partial<Instance>) =>
  ({ pool: "web", state: "reserved", start_attempts: 0, last_error: null, ...over }) as Instance;

describe("launchGate", () => {
  it("is off without a pool, while checking, and while a request is in flight", () => {
    expect(launchGate(undefined, undefined, false).enabled).toBe(false);
    expect(launchGate(pool, undefined, false).reason).toMatch(/Checking/);
    expect(launchGate(pool, cap(), true).enabled).toBe(false);
  });
  it("shows the node's blockers instead of a generic error", () => {
    const gate = launchGate(
      pool,
      cap({ launchable: false, blockers: ["docker unreachable"] }),
      false,
    );
    expect(gate).toEqual({ enabled: false, reason: "docker unreachable" });
  });
  it("is off at the cap and on when launchable", () => {
    expect(launchGate(pool, cap({ free_slots: 0 }), false).enabled).toBe(false);
    expect(launchGate(pool, cap(), false).enabled).toBe(true);
  });
});

describe("launchStatus", () => {
  it("does not call a request a success", () => {
    expect(launchStatus(pool, [])).toMatch(/requested/);
  });
  it("reports start errors and progress", () => {
    expect(
      launchStatus(pool, [inst({ last_error: "image not found", start_attempts: 2 })]),
    ).toMatch(/failed to start \(attempt 2\): image not found/);
    expect(launchStatus(pool, [inst({})])).toBe("starting…");
    expect(launchStatus(pool, [inst({ state: "running" })])).toBe("up to date");
  });
});
