// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Agents } from "./Agents";

const net = vi.hoisted(() => {
  const net = {
    routes: {} as Record<string, unknown>,
    errors: {} as Record<string, string>,
  };
  const Base = globalThis.Request;
  globalThis.Request = class extends Base {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(
        typeof input === "string" && input.startsWith("/") ? `http://localhost${input}` : input,
        init,
      );
    }
  } as typeof Request;
  globalThis.fetch = (async (input: Request) => {
    const path = new URL(input.url).pathname;
    if (net.errors[path]) {
      return new Response(JSON.stringify({ detail: net.errors[path] }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }
    const body = net.routes[path];
    return new Response(JSON.stringify(body ?? []), {
      headers: { "Content-Type": "application/json" },
    });
  }) as typeof fetch;
  return net;
});

// No event stream in tests: load once.
vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  return {
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

const emptyHygiene = {
  as_of: "2026-10-16T00:00:00Z",
  totals: { lost: 0, gone: 0, archived: 0, lost_holding_claims: 0 },
  groups: [],
  claim_holders: [],
  lost_spike: {
    window_hours: 24,
    current: 0,
    previous: 0,
    min_count: 5,
    factor: 2,
    triggered: false,
  },
  age_buckets: {
    lost: { under_1d: 0, "1d_to_7d": 0, "7d_to_30d": 0, "30d_and_over": 0 },
    gone: { under_1d: 0, "1d_to_7d": 0, "7d_to_30d": 0, "30d_and_over": 0 },
  },
};

const baseRoutes = {
  "/api/pools": [],
  "/api/agents": [],
  "/api/instances": [],
  "/api/fleet/capabilities": { public_url_configured: true, targets: [], pools: [] },
  "/api/fleet/templates": {
    public_url_configured: true,
    default_node: "local",
    pools: 0,
    nodes: [],
    templates: [],
  },
  "/api/agents/hygiene": emptyHygiene,
};

describe("Agents on a fresh install", () => {
  it("offers the guided first worker instead of sending the user to a raw pool form", async () => {
    net.errors = {};
    net.routes = { ...baseRoutes };
    render(<Agents />);
    expect(await screen.findByText("New worker")).toBeTruthy();
    expect(screen.queryByText(/Create one in the Pools view first/)).toBeNull();
    expect(screen.queryByText(/Worker bootstrap token/)).not.toBeNull(); // still there
  });
});

describe("Agents hygiene", () => {
  it("renders lost and gone totals, buckets and groups", async () => {
    net.errors = {};
    net.routes = {
      ...baseRoutes,
      "/api/agents/hygiene": {
        ...emptyHygiene,
        totals: { lost: 2, gone: 1, archived: 0, lost_holding_claims: 1 },
        groups: [
          {
            pool: "workers",
            state: "lost",
            day: "2026-10-15",
            count: 2,
            oldest_heartbeat_at: "2026-10-14T00:00:00Z",
            newest_heartbeat_at: "2026-10-15T00:00:00Z",
            max_age_hours: 48,
          },
        ],
        claim_holders: [{ agent_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", task_id: "t1" }],
        lost_spike: {
          window_hours: 24,
          current: 6,
          previous: 1,
          min_count: 5,
          factor: 2,
          triggered: true,
        },
        age_buckets: {
          lost: { under_1d: 1, "1d_to_7d": 1, "7d_to_30d": 0, "30d_and_over": 0 },
          gone: { under_1d: 0, "1d_to_7d": 1, "7d_to_30d": 0, "30d_and_over": 0 },
        },
      },
    };
    render(<Agents />);
    // The heading renders while the totals load; wait for the data itself.
    expect(await screen.findByText(/2 lost · 1 gone · 0 archived/)).toBeTruthy();
    expect(screen.getByText("Lost and gone")).toBeTruthy();
    expect(screen.getByText(/6 agents lost in the last 24h/)).toBeTruthy();
    expect(screen.getByText("workers")).toBeTruthy();
    expect(screen.getByText(/Lost agents holding a claimed task/)).toBeTruthy();
  });

  it("shows empty groups without inventing zeros for a missing source", async () => {
    net.errors = {};
    net.routes = { ...baseRoutes };
    render(<Agents />);
    expect(await screen.findByText("No lost or gone identities.")).toBeTruthy();
  });

  it("shows the hygiene error instead of a zero table", async () => {
    net.errors = { "/api/agents/hygiene": "boom" };
    net.routes = { ...baseRoutes };
    render(<Agents />);
    expect(await screen.findByText(/Lost and gone failed to load/)).toBeTruthy();
    expect(screen.queryByText("No lost or gone identities.")).toBeNull();
  });
});
