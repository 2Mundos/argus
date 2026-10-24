import { describe, expect, it } from "vitest";
import {
  costLabel,
  money,
  notReportedLabel,
  pricingNote,
  share,
  tokensOrDash,
  topFive,
} from "./cost";

const f = (o: Partial<Parameters<typeof costLabel>[0]> = {}) => ({
  runs: 1,
  measured_runs: 1,
  not_reported_runs: 0,
  input_tokens: 10,
  output_tokens: 10,
  cached_input_tokens: 0,
  estimated_cost_usd: 1,
  reported_cost_usd: null,
  unpriced_runs: 0,
  ...o,
});

describe("cost helpers", () => {
  it("renders a missing figure as a dash, a real zero as 0", () => {
    expect(tokensOrDash(null)).toBe("—");
    expect(tokensOrDash(undefined)).toBe("—");
    expect(tokensOrDash(0)).toBe("0");
    expect(tokensOrDash(12_000)).toBe("12k");
  });

  it("never prints an unpriced group as zero cost", () => {
    expect(costLabel(f({ estimated_cost_usd: null, unpriced_runs: 2 }), "USD")).toBe("unpriced");
    expect(costLabel(f({ estimated_cost_usd: null, input_tokens: null }), "USD")).toBe("—");
    expect(costLabel(f({ estimated_cost_usd: 1.234, unpriced_runs: 1 }), "USD")).toBe(
      "1.23 USD (partial)",
    );
    expect(costLabel(f({ estimated_cost_usd: 0 }), "USD")).toBe("0.00 USD");
    expect(money(0.001, "EUR")).toBe("<0.01 EUR");
  });

  it("labels not_reported runs only when there are some", () => {
    expect(notReportedLabel(f())).toBeNull();
    expect(notReportedLabel(f({ not_reported_runs: 3 }))).toBe("3 not reported");
  });

  it("takes the top five among groups with measured runs", () => {
    const rows = [1, 2, 3, 4, 5, 6].map((n) => ({ n, measured_runs: 1 }));
    rows.splice(2, 0, { n: 0, measured_runs: 0 });
    expect(topFive(rows).map((r) => r.n)).toEqual([1, 2, 3, 4, 5]);
    expect(topFive([{ measured_runs: 0 }])).toEqual([]);
  });

  it("scales bars to the largest group, and gives no bar without a figure", () => {
    const big = f({ estimated_cost_usd: 4 });
    const small = f({ estimated_cost_usd: 1 });
    const unpriced = f({ estimated_cost_usd: null });
    const rows = [big, small, unpriced];
    expect(share(big, rows)).toBe(1);
    expect(share(small, rows)).toBe(0.25);
    expect(share(unpriced, rows)).toBe(0);
  });

  it("cites the source, date and unpriced models", () => {
    const note = pricingNote({
      currency: "USD",
      as_of: "2026-10-20",
      version: "v1",
      source: "argus_api/accounts/pricing.json",
      estimate: true,
      unpriced_models: ["mystery-1"],
    });
    expect(note).toContain("argus_api/accounts/pricing.json");
    expect(note).toContain("2026-10-20");
    expect(note).toContain("mystery-1");
  });
});
