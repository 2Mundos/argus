// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Home } from "./Home";

const mock = vi.hoisted(() => ({
  overview: undefined as unknown,
  overviewError: undefined as Error | undefined,
}));

vi.mock("../api", () => ({
  api: { GET: (path: string) => path },
  call: async (path: string) => {
    if (path === "/api/overview") {
      if (mock.overviewError) throw mock.overviewError;
      return mock.overview;
    }
    if (path === "/api/system") {
      return {
        readiness: { status: "ready", checks: { database: { status: "ok" } } },
        loops: {},
      };
    }
    return [];
  },
}));

vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  return {
    useLive: () => ({ status: "live", version: 0 }),
    useLiveData: <T,>(load: () => Promise<T>) => {
      const [data, setData] = useState<T>();
      const [error, setError] = useState<Error>();
      useEffect(() => {
        load().then(setData, setError);
      }, []);
      return { data, error, reload: () => {} };
    },
  };
});

afterEach(cleanup);

const zeros = (...keys: string[]) => Object.fromEntries(keys.map((k) => [k, 0]));

describe("Home", () => {
  it("shows measured zeros and all states, not an error", async () => {
    mock.overviewError = undefined;
    mock.overview = {
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
        lost_holding_claims: 0,
        lost_spike: {
          window_hours: 24,
          current: 0,
          previous: 0,
          min_count: 5,
          factor: 2,
          triggered: false,
        },
        hygiene_by_pool: {},
      },
      pools: {
        by_status: zeros("ok"),
        total: 0,
        desired: 0,
        max: 0,
        live: 0,
        running: 0,
        items: [],
      },
      instances: { by_state: zeros("running"), draining: 0 },
      accounts: {
        by_state: zeros("available"),
        total: 0,
        by_provider: {},
        next_reset_at: null,
        awaiting_quota: false,
        quota_measured: false,
      },
      decisions: { by_state: zeros("open"), open_by_kind: {}, open_by_audience: {} },
      knowledge: { pages_by_state: {}, proposals_by_state: zeros("open") },
    };
    render(<Home />);
    expect(await screen.findByText("Nothing needs attention right now.")).toBeTruthy();
    expect(screen.getByText("Quota is not measured by Argus.")).toBeTruthy();
    expect(screen.getByText("All states")).toBeTruthy();
  });

  it("does not claim all-clear or show zeros when totals fail", async () => {
    mock.overviewError = new Error("boom");
    render(<Home />);
    expect(await screen.findByText("Totals unavailable.")).toBeTruthy();
    expect(screen.getByText(/Totals failed to load: boom/)).toBeTruthy();
    expect(screen.queryByText("Nothing needs attention right now.")).toBeNull();
    expect(screen.getByText("Not an all-clear: some sources could not be read.")).toBeTruthy();
  });
});
