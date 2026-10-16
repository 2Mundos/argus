// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Agents } from "./Agents";

const net = vi.hoisted(() => {
  const net = { routes: {} as Record<string, unknown> };
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
    const body = net.routes[new URL(input.url).pathname];
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
      useEffect(() => {
        load().then(setData);
      }, []);
      return { data, error: undefined, reload: () => {} };
    },
  };
});

afterEach(cleanup);

describe("Agents on a fresh install", () => {
  it("offers the guided first worker instead of sending the user to a raw pool form", async () => {
    net.routes = {
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
    };
    render(<Agents />);
    expect(await screen.findByText("New worker")).toBeTruthy();
    expect(screen.queryByText(/Create one in the Pools view first/)).toBeNull();
    expect(screen.queryByText(/Worker bootstrap token/)).not.toBeNull(); // still there
  });
});
