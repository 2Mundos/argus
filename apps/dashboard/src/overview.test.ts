import { describe, expect, it } from "vitest";
import type { SystemSummary } from "./api";
import { attention, type Overview, type Source, sourceStatus, sum, verdict } from "./overview";

const zeros = (...keys: string[]) => Object.fromEntries(keys.map((k) => [k, 0]));

function overview(patch: Partial<Overview> = {}): Overview {
  return {
    as_of: "2026-10-16T00:00:00Z",
    tasks: zeros(
      "awaiting_approval",
      "pending",
      "ready",
      "claimed",
      "blocked",
      "in_review",
      "succeeded",
      "failed",
      "cancelled",
    ),
    agents: {
      by_state: zeros("active", "draining", "gone", "lost"),
      by_role: {},
      holding_claims: 0,
      lost_on_unfinished_instance: 0,
    },
    pools: {
      by_status: zeros("ok", "unreachable", "failing"),
      total: 0,
      desired: 0,
      max: 0,
      live: 0,
      running: 0,
      items: [],
    },
    instances: { by_state: zeros("reserved", "running", "terminating", "terminated"), draining: 0 },
    accounts: {
      by_state: zeros("available", "exhausted", "disabled"),
      total: 0,
      by_provider: {},
      next_reset_at: null,
      quota_measured: false,
    },
    decisions: {
      by_state: zeros("open", "answered", "cancelled"),
      open_by_kind: {},
      open_by_audience: {},
    },
    knowledge: { pages_by_state: {}, proposals_by_state: zeros("open") },
    ...patch,
  };
}

function system(checks: Record<string, string> = { database: "ok" }): SystemSummary {
  return {
    readiness: {
      status: "ready",
      checks: Object.fromEntries(Object.entries(checks).map(([k, status]) => [k, { status }])),
    },
    loops: {},
  } as unknown as SystemSummary;
}

const ok = <T>(data: T): Source<T> => ({ data });

describe("attention", () => {
  it("is empty only when every source loaded and nothing is wrong", () => {
    const a = attention({ stream: "live", overview: ok(overview()), system: ok(system()) });
    expect(a.items).toEqual([]);
    expect(a.unavailable).toEqual([]);
    expect(a.pending).toEqual([]);
  });

  it("never reads as all-clear when a source failed", () => {
    const a = attention({ stream: "live", overview: { error: "boom" }, system: ok(system()) });
    expect(a.unavailable).toEqual(["Totals"]);
    expect(a.items.some((i) => i.id === "unavailable-Totals")).toBe(true);
  });

  it("reports sources still loading as pending, not as zero", () => {
    const a = attention({ stream: "live", overview: {}, system: {} });
    expect(a.pending).toEqual(["System health", "Totals"]);
    expect(a.items).toEqual([]);
  });

  it("does not turn lost history into a current alert", () => {
    const o = overview();
    o.agents.by_state.lost = 40;
    o.instances.by_state.terminated = 40;
    const a = attention({ stream: "live", overview: ok(o), system: ok(system()) });
    expect(a.items).toEqual([]);
  });

  it("flags lost agents on instances not yet terminated", () => {
    const o = overview();
    o.agents.lost_on_unfinished_instance = 2;
    const a = attention({ stream: "live", overview: ok(o), system: ok(system()) });
    expect(a.items.map((i) => i.id)).toEqual(["lost-unfinished"]);
  });

  it("treats failed tasks as info, never critical", () => {
    const o = overview();
    o.tasks.failed = 9;
    const a = attention({ stream: "live", overview: ok(o), system: ok(system()) });
    expect(a.items).toHaveLength(1);
    expect(a.items[0]?.severity).toBe("info");
  });

  it("orders critical before attention before info, humans first among equals", () => {
    const o = overview();
    o.tasks.failed = 1;
    o.tasks.blocked = 5;
    o.decisions.by_state.open = 1;
    o.pools.items = [{ name: "p", status: "failing", desired: 1, max: 2, live: 0, running: 0 }];
    const a = attention({ stream: "live", overview: ok(o), system: ok(system()) });
    expect(a.items.map((i) => i.id)).toEqual(["pool-p", "decisions-open", "blocked", "failed"]);
  });

  it("flags failing checks as critical and degraded as attention", () => {
    const a = attention({
      stream: "live",
      overview: ok(overview()),
      system: ok(system({ database: "failing", secrets: "degraded" })),
    });
    expect(a.items.map((i) => [i.id, i.severity])).toEqual([
      ["check-database", "critical"],
      ["check-secrets", "attention"],
    ]);
  });

  it("warns when the stream is offline", () => {
    const a = attention({ stream: "offline", overview: ok(overview()), system: ok(system()) });
    expect(a.items.map((i) => i.id)).toEqual(["stream"]);
  });

  it("flags a provider with accounts but none available", () => {
    const o = overview();
    o.accounts.by_provider = { anthropic: { available: 0, exhausted: 1, disabled: 0 } };
    const a = attention({ stream: "live", overview: ok(o), system: ok(system()) });
    expect(a.items[0]?.id).toBe("provider-anthropic");
    expect(a.items[0]?.severity).toBe("critical");
  });
});

describe("distinct zero / unknown / error", () => {
  it("separates loading, ok, stale and error", () => {
    expect(sourceStatus({}, true)).toBe("loading");
    expect(sourceStatus({ error: "x" }, true)).toBe("error");
    expect(sourceStatus({ data: 1 }, true)).toBe("ok");
    expect(sourceStatus({ data: 1 }, false)).toBe("stale");
    expect(sourceStatus({ data: 1, error: "x" }, true)).toBe("stale");
  });

  it("sums to unknown when a state is missing, and to 0 when all are measured zero", () => {
    expect(sum({ a: 0, b: 0 }, ["a", "b"])).toBe(0);
    expect(sum({ a: 1 }, ["a", "b"])).toBeUndefined();
    expect(sum(undefined, ["a"])).toBeUndefined();
  });

  it("verdict is unknown without data, never ok", () => {
    expect(verdict(undefined)).toBe("unknown");
    expect(verdict(system({}))).toBe("unknown");
    expect(verdict(system({ a: "ok" }))).toBe("ok");
    expect(verdict(system({ a: "ok", b: "degraded" }))).toBe("degraded");
    expect(verdict(system({ a: "degraded", b: "failing" }))).toBe("failing");
  });
});
