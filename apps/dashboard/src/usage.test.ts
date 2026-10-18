import { describe, expect, it } from "vitest";
import {
  hasModelUsage,
  poolState,
  type QuotaWindow,
  quotaFull,
  tokens,
  windowLabel,
  windowQuery,
} from "./usage";

const quota = (q: Partial<QuotaWindow>): QuotaWindow => ({
  window: "300m",
  window_minutes: 300,
  used_percent: null,
  resets_at: null,
  exhausted_until: null,
  observed_at: "2026-10-18T10:00:00Z",
  source: "polled",
  ...q,
});

describe("windowLabel", () => {
  it("names timed windows by duration", () => {
    expect(windowLabel({ window: "300m", window_minutes: 300 })).toBe("5h");
    expect(windowLabel({ window: "10080m", window_minutes: 10_080 })).toBe("Weekly");
    expect(windowLabel({ window: "1440m", window_minutes: 1440 })).toBe("1d");
    expect(windowLabel({ window: "45m", window_minutes: 45 })).toBe("45 min");
  });
  it("names pools", () => {
    expect(windowLabel({ window: "third_party", window_minutes: null })).toBe("Third-party models");
    expect(windowLabel({ window: "new_pool", window_minutes: null })).toBe("new pool");
  });
});

describe("windowQuery", () => {
  const now = new Date("2026-10-18T15:30:00Z");
  it("uses hours up to a day and days beyond", () => {
    expect(windowQuery("24h", now)).toEqual({
      from: "2026-10-17T15:30:00.000Z",
      to: "2026-10-18T15:30:00.000Z",
      bucket: "hour",
    });
    expect(windowQuery("7d", now).bucket).toBe("day");
  });
  it("starts today at local midnight and all time at creation", () => {
    const today = new Date(windowQuery("today", now).from);
    expect([today.getHours(), today.getMinutes()]).toEqual([0, 0]);
    expect(windowQuery("all", now, "2026-01-02T00:00:00Z").from).toBe("2026-01-02T00:00:00.000Z");
  });
});

describe("quota", () => {
  const now = Date.parse("2026-10-18T12:00:00Z");
  it("is full only at 100% or while a pool is exhausted", () => {
    expect(quotaFull(quota({ used_percent: 99 }), now)).toBe(false);
    expect(quotaFull(quota({ used_percent: 100 }), now)).toBe(true);
    const pool = { window: "cursor", window_minutes: null };
    expect(quotaFull(quota({ ...pool, exhausted_until: "2026-11-03T00:00:00Z" }), now)).toBe(true);
    expect(poolState(quota({ ...pool, exhausted_until: "2026-10-01T00:00:00Z" }), now)).toBe(
      "within",
    );
  });
});

describe("formatting", () => {
  it("shortens token counts", () => {
    expect([tokens(999), tokens(1234), tokens(45_600), tokens(3_400_000)]).toEqual([
      "999",
      "1.2k",
      "46k",
      "3.4M",
    ]);
  });
  it("leaves repository accounts out of usage", () => {
    expect(hasModelUsage("gitea")).toBe(false);
    expect(hasModelUsage("cursor")).toBe(true);
  });
});
