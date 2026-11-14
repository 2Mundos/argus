// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CostFigures, CostReport } from "../cost";
import { Cost, Report } from "./Cost";

const none: CostFigures = {
  runs: 0,
  measured_runs: 0,
  not_reported_runs: 0,
  reported_rows: 0,
  total_rows: 0,
  coverage_percent: null,
  attempts: 0,
  measured_attempts: 0,
  not_reported_attempts: 0,
  input_tokens: null,
  output_tokens: null,
  cached_input_tokens: null,
  estimated_cost_usd: null,
  reported_cost_usd: null,
  unpriced_runs: 0,
};
const used: CostFigures = {
  ...none,
  runs: 3,
  measured_runs: 2,
  not_reported_runs: 1,
  reported_rows: 2,
  total_rows: 3,
  coverage_percent: 66.7,
  input_tokens: 2_000,
  output_tokens: 1_000,
  cached_input_tokens: 500,
  estimated_cost_usd: 1.5,
};
const unknown: CostReport["by_account"][number]["quota_status"] = {
  state: "unknown",
  reason: "no_usage_records",
  windows: [],
};

const REPORT: CostReport = {
  window: "7d",
  start: "2026-10-13T12:00:00Z",
  end: "2026-10-20T12:00:00Z",
  tz: "UTC",
  pricing: {
    currency: "USD",
    as_of: "2026-10-20",
    version: "v1",
    source: "argus_api/accounts/pricing.json",
    estimate: true,
    unpriced_models: ["mystery-1"],
  },
  totals: {
    ...used,
    runs: 6,
    not_reported_runs: 4,
    total_rows: 6,
    coverage_percent: 33.3,
    attempts: 3,
    measured_attempts: 2,
  },
  unattributed: { ...none },
  by_account: [
    {
      ...used,
      account_id: "a1",
      name: "codex-main",
      provider: "openai",
      quota: [
        {
          window: "300m",
          window_minutes: 300,
          used_percent: 42,
          resets_at: null,
          exhausted_until: null,
          observed_at: "2026-10-20T11:00:00Z",
          source: "reported",
        },
      ],
      quota_status: { state: "known", reason: null, windows: [] },
    },
    {
      ...none,
      runs: 4,
      not_reported_runs: 4,
      total_rows: 4,
      coverage_percent: 0,
      account_id: "a2",
      name: "nr-only",
      provider: "openai",
      quota: [],
      quota_status: unknown,
    },
    {
      ...none,
      account_id: "a3",
      name: "idle",
      provider: "openai",
      quota: [],
      quota_status: unknown,
    },
    {
      ...used,
      estimated_cost_usd: null,
      unpriced_runs: 2,
      account_id: "a4",
      name: "unpriced-acc",
      provider: "openai",
      quota: [],
      quota_status: unknown,
    },
  ],
  by_project: [
    { ...used, project: "alpha" },
    {
      ...none,
      runs: 1,
      measured_runs: 1,
      reported_rows: 1,
      total_rows: 1,
      coverage_percent: 100,
      input_tokens: 0,
      output_tokens: 0,
      project: null,
    },
  ],
  orchestrator: {
    ...none,
    runs: 2,
    measured_runs: 2,
    reported_rows: 2,
    total_rows: 2,
    coverage_percent: 100,
    input_tokens: 400,
    output_tokens: 100,
    estimated_cost_usd: 0.05,
  },
  daily: [
    { ...none, start: "2026-10-19T00:00:00Z" },
    { ...used, start: "2026-10-20T00:00:00Z" },
  ],
};

const mock = vi.hoisted(() => ({ queries: [] as Record<string, string>[] }));

vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  return {
    useLiveData: <T,>(load: () => Promise<T>, deps: unknown[]) => {
      const [data, setData] = useState<T>();
      useEffect(() => {
        load().then(setData);
      }, deps);
      return { data, error: undefined, reload: () => {} };
    },
  };
});
vi.mock("../api", () => ({
  api: {
    GET: (_path: string, init: { params: { query: Record<string, string> } }) => {
      mock.queries.push(init.params.query);
      return init.params.query;
    },
  },
  call: async (q: Record<string, string>) => ({ ...REPORT, window: q.window }),
}));

afterEach(() => {
  cleanup();
  mock.queries.length = 0;
});

const row = (name: string) => screen.getByText(name).closest("tr") as HTMLElement;

describe("Cost view", () => {
  it("shows tiles labelled as an estimate with the currency", async () => {
    render(<Cost />);
    await screen.findByText("Cost (lower bound, estimate, USD)");
    expect(screen.getAllByText("1.50 USD").length).toBeGreaterThan(0);
    expect(mock.queries[0]).toMatchObject({ window: "7d" });
  });

  it("never renders not_reported or missing figures as 0", async () => {
    render(<Cost />);
    await screen.findByText("nr-only");
    const nr = within(row("nr-only"));
    expect(nr.getAllByText("—").length).toBeGreaterThanOrEqual(4);
    expect(nr.getByText("4 not reported")).toBeTruthy();
    expect(nr.queryByText("0")).toBeNull();
    const idle = within(row("idle"));
    expect(idle.getByText("no usage")).toBeTruthy();
    expect(idle.queryByText("0")).toBeNull();
  });

  it("marks an unpriced group and keeps a measured zero as 0", async () => {
    render(<Cost />);
    await screen.findByText("unpriced-acc");
    expect(within(row("unpriced-acc")).getByText("unpriced")).toBeTruthy();
    expect(within(row("no project")).getAllByText("0").length).toBe(2);
  });

  it("shows quota status through the Accounts component", async () => {
    render(<Cost />);
    await screen.findByText("idle");
    expect(within(row("idle")).getByText("No usage reported yet")).toBeTruthy();
  });

  it("renders the provider quota meter like Accounts", async () => {
    render(<Cost />);
    await screen.findByText("codex-main");
    expect(within(row("codex-main")).getByText("58% left")).toBeTruthy();
  });

  it("badges the top five and names the pricing source, date and unpriced models", async () => {
    render(<Cost />);
    await screen.findByText("codex-main");
    expect(within(row("codex-main")).getByText("top 1")).toBeTruthy();
    expect(within(row("nr-only")).queryByText(/^top/)).toBeNull();
    const note = screen.getByTestId("pricing-note").textContent ?? "";
    expect(note).toContain("argus_api/accounts/pricing.json");
    expect(note).toContain("2026-10-20");
    expect(note).toContain("mystery-1");
  });

  it("labels totals a lower bound and shows coverage when it is below 100%", async () => {
    render(<Cost />);
    await screen.findByText("codex-main");
    const totals = screen.getByTestId("totals-coverage").textContent ?? "";
    expect(totals).toContain("coverage 33.3% (2 of 6 runs)");
    expect(totals).toContain("lower bound");
    expect(screen.getByText("Input (lower bound)")).toBeTruthy();
    expect(within(row("codex-main")).getByText(/coverage 66.7% \(2 of 3 runs\)/)).toBeTruthy();
    expect(within(row("codex-main")).getByText(/lower bound/)).toBeTruthy();
  });

  it("shows no lower-bound label at full coverage", () => {
    const totals = { ...used, runs: 2, not_reported_runs: 0, total_rows: 2, coverage_percent: 100 };
    const full = { ...REPORT, totals };
    render(<Report report={full} tz="UTC" />);
    expect(screen.getByTestId("totals-coverage").textContent).toBe("coverage 100% (2 of 2 runs)");
    // Only the totals are checked: the fixture's table rows still have partial coverage.
    expect(screen.queryByText(/totals are a lower bound/)).toBeNull();
    expect(screen.getByText("Cost (estimate, USD)")).toBeTruthy();
    expect(screen.queryByText("Input (lower bound)")).toBeNull();
  });

  it("shows 0% coverage and no zero total for a layer with no reported rows", async () => {
    render(<Cost />);
    await screen.findByText("nr-only");
    const nr = within(row("nr-only"));
    expect(nr.getByText(/coverage 0% \(0 of 4 runs\)/)).toBeTruthy();
    expect(nr.queryByText("0")).toBeNull();
    expect(nr.queryByText(/^0\.00/)).toBeNull();
    // A layer with no runs has unknown coverage, not 0%.
    expect(within(row("idle")).queryByText(/coverage/)).toBeNull();
  });

  it("reloads for another period", async () => {
    render(<Cost />);
    await screen.findByText("codex-main");
    fireEvent.click(screen.getByRole("button", { name: "30 days" }));
    await waitFor(() => expect(mock.queries.at(-1)).toMatchObject({ window: "30d" }));
  });
});
