// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ArgusEvent } from "../api";
import type { TaskCost } from "../cost";
import { TaskCostTile } from "./TaskCost";

const mock = vi.hoisted(() => ({
  cost: {} as unknown,
  accepts: undefined as undefined | ((event: ArgusEvent) => boolean),
}));
vi.mock("../api", () => ({ api: { GET: (path: string) => path }, call: async () => mock.cost }));
vi.mock("../live", async () => {
  const React = await import("react");
  return {
    useLiveData: (
      load: () => Promise<unknown>,
      _deps: unknown[],
      options?: { accepts?: (event: ArgusEvent) => boolean },
    ) => {
      mock.accepts = options?.accepts;
      const [data, setData] = React.useState<unknown>();
      React.useEffect(() => {
        load().then(setData);
      }, []);
      return { data, reload: () => {} };
    },
  };
});

const figures = (o: Partial<TaskCost["totals"]> = {}): TaskCost["totals"] => ({
  runs: 2,
  measured_runs: 2,
  not_reported_runs: 0,
  input_tokens: 2000,
  output_tokens: 500,
  cached_input_tokens: 0,
  estimated_cost_usd: 1.2,
  reported_cost_usd: null,
  unpriced_runs: 0,
  ...o,
});

const cost = (o: Partial<TaskCost> = {}): TaskCost => ({
  task_id: "t1",
  budget_usd: null,
  budget_state: "none",
  partial: false,
  pricing: {
    currency: "USD",
    as_of: "2026-10-01",
    version: "v1",
    source: "shipped",
    unpriced_models: [],
  },
  totals: figures(),
  by_attempt: [
    { attempt: 1, models: ["claude-opus-5-5"], ...figures({ runs: 1, estimated_cost_usd: 0.4 }) },
    { attempt: 2, models: [], ...figures({ runs: 1, estimated_cost_usd: null, unpriced_runs: 1 }) },
  ],
  ...o,
});

async function show(c: TaskCost) {
  mock.cost = c;
  await act(async () => {
    render(<TaskCostTile taskId="t1" />);
  });
}

afterEach(cleanup);

describe("TaskCostTile", () => {
  it("labels the estimate and lists each attempt with its models and runs", async () => {
    await show(cost({ totals: figures({ reported_cost_usd: 1.5 }) }));
    const line = screen.getByRole("region", { name: "Cost" }).querySelector("p")?.textContent;
    expect(line).toBe("1.20 USD estimate · 1.50 USD reported · 2.0k in · 500 out · 2 runs");
    expect(screen.queryByRole("meter")).toBeNull();
    const rows = screen.getAllByRole("row").map((r) => r.textContent);
    expect(rows[1]).toBe("#1claude-opus-5-512.0k / 5000.40 USD");
    expect(rows[2]).toBe("#2—12.0k / 500unpriced");
  });

  it("marks a partial figure and colours the budget bar by its state", async () => {
    await show(cost({ partial: true, budget_usd: 1.5, budget_state: "partial_warning" }));
    expect(screen.getByText("partial")).toBeTruthy();
    const meter = screen.getByRole("meter", { name: "Budget used" });
    expect(meter.getAttribute("aria-valuetext")).toBe("1.20 USD of 1.50 USD budget");
    expect(meter.parentElement?.className).toBe("task-budget budget-warning");
    cleanup();
    await show(cost({ budget_usd: 1, budget_state: "exhausted" }));
    const spent = screen.getByRole("meter", { name: "Budget used" });
    expect(spent.parentElement?.className).toBe("task-budget budget-exhausted");
    expect(spent.getAttribute("aria-valuenow")).toBe("1");
    expect(screen.getByText(/· exhausted/)).toBeTruthy();
  });

  it("reloads on the task's own events, task.cost included", async () => {
    await show(cost());
    const event = (subject_id: string, kind = "task.cost") =>
      ({
        id: 1,
        kind,
        subject_id,
        subject_type: "task",
        actor: "system",
        at: "",
        data: {},
      }) as ArgusEvent;
    expect(mock.accepts?.(event("t1"))).toBe(true);
    expect(mock.accepts?.(event("t2"))).toBe(false);
  });
});
