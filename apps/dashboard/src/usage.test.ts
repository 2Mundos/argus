import { describe, expect, it } from "vitest";
import {
  allUnmeasured,
  bucketTokens,
  measuredRuns,
  notMeasured,
  probeNote,
  reasonsNote,
  roleSplit,
  snapshotStale,
  tokensOrDash,
  hasModelUsage,
  parseTokenLimits,
  poolState,
  type QuotaWindow,
  quotaFull,
  quotaUnknownLabel,
  tokens,
  unmeasuredNote,
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

describe("quota status and measurement helpers", () => {
  it("labels every unknown reason and never leaves it mute", () => {
    expect(quotaUnknownLabel({ reason: "no_usage_records" })).toBe("No usage reported yet");
    expect(quotaUnknownLabel({ reason: "provider_does_not_expose" })).toBe(
      "Provider does not expose quota",
    );
    expect(quotaUnknownLabel({ reason: "window_not_configured" })).toBe(
      "No quota window configured",
    );
    expect(quotaUnknownLabel({ reason: null })).toBe("Quota unknown");
  });

  it("counts runs without measurement", () => {
    expect(unmeasuredNote(0)).toBeNull();
    expect(unmeasuredNote(undefined)).toBeNull();
    expect(unmeasuredNote(1)).toBe("1 run without measurement");
    expect(unmeasuredNote(3)).toBe("3 runs without measurement");
    expect(allUnmeasured({ runs: 3, unmeasured_runs: 3 })).toBe(true);
    expect(allUnmeasured({ runs: 3, unmeasured_runs: 1 })).toBe(false);
    expect(allUnmeasured({ runs: 0, unmeasured_runs: 0 })).toBe(false);
    expect(allUnmeasured({})).toBe(false);
  });

  it("parses token limits: positive integers only, all empty clears", () => {
    const empty = { "5h": "", "24h": "", "7d": "" };
    expect(parseTokenLimits(empty)).toEqual({ limits: null });
    expect(parseTokenLimits({ ...empty, "24h": " 2000 " })).toEqual({ limits: { "24h": 2000 } });
    for (const bad of ["0", "-1", "1.5", "abc"])
      expect(parseTokenLimits({ ...empty, "5h": bad })).toHaveProperty("error");
  });
});

describe("telemetry honesty helpers", () => {
  it("names a sentence per reason code, with the provider", () => {
    expect(quotaUnknownLabel({ reason: "unsupported_provider" }, "anthropic")).toBe(
      "Claude does not expose a quota Argus can read",
    );
    expect(quotaUnknownLabel({ reason: "unsupported_provider" }, "acme")).toBe(
      "acme does not expose a quota Argus can read",
    );
    expect(quotaUnknownLabel({ reason: "unsupported_provider" })).toBe(
      "This provider does not expose a quota Argus can read",
    );
    for (const reason of [
      "api_key_account",
      "not_yet_probed",
      "probe_failed",
      "probe_skipped",
      "probe_rate_limited",
    ] as const) {
      expect(quotaUnknownLabel({ reason })).not.toBe("Quota unknown");
    }
  });

  it("tells a measured zero from not measured", () => {
    expect(notMeasured({ runs: 3, unmeasured_runs: 3, measured_runs: 0 })).toBe(true);
    expect(notMeasured({ runs: 3, unmeasured_runs: 3 })).toBe(true); // older server
    expect(notMeasured({ runs: 3, unmeasured_runs: 1, measured_runs: 2 })).toBe(false);
    expect(notMeasured({ runs: 0, measured_runs: 0 })).toBe(false); // nothing ran
    expect(measuredRuns({ runs: 5, unmeasured_runs: 2 })).toBe(3);
    expect(tokensOrDash({ runs: 2, unmeasured_runs: 2 }, 0)).toBe("—");
    expect(tokensOrDash({ runs: 2, unmeasured_runs: 1 }, 0)).toBe("0");
    const b = (runs: number, unmeasured_runs: number) =>
      ({
        start: "x",
        input_tokens: 0,
        output_tokens: 0,
        cached_input_tokens: 0,
        cost_usd: 0,
        runs,
        unmeasured_runs,
      }) as Parameters<typeof bucketTokens>[0];
    expect(bucketTokens(b(0, 0))).toBe(0); // no rows: a measured zero
    expect(bucketTokens(b(2, 2))).toBeNull(); // all unmeasured: a gap
  });

  it("summarises reasons and roles", () => {
    expect(reasonsNote({})).toBeNull();
    expect(reasonsNote({ unknown: 1, no_usage_in_output: 2 })).toBe(
      "no usage in output ×2, unknown ×1",
    );
    expect(roleSplit({})).toBeNull();
    expect(roleSplit({ unknown: 1, executor: 2, engine: 3 })).toBe(
      "Harness 3 · Executor 2 · Role unknown 1",
    );
  });

  it("judges a snapshot stale by age and by an elapsed reset", () => {
    const now = Date.parse("2026-10-18T12:00:00Z");
    expect(snapshotStale(quota({ observed_at: "2026-10-18T11:30:00Z" }), now)).toBe(false);
    expect(snapshotStale(quota({ observed_at: "2026-10-18T10:30:00Z" }), now)).toBe(true);
    expect(
      snapshotStale(
        quota({ observed_at: "2026-10-18T11:59:00Z", resets_at: "2026-10-18T11:00:00Z" }),
        now,
      ),
    ).toBe(true);
  });

  it("describes the last probe", () => {
    expect(probeNote(null, "now")).toBeNull();
    expect(
      probeNote({ status: "skipped", reason: "codex binary not configured", at: "x" }, "2h ago"),
    ).toBe("Last read skipped (codex binary not configured), 2h ago");
    expect(probeNote({ status: "updated", reason: null, at: "x" }, "1m ago")).toBe(
      "Last read updated, 1m ago",
    );
  });
});
