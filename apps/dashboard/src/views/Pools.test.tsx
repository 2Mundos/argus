// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Pool } from "../api";
import { Pools } from "./Pools";

const pool = (over: Partial<Pool> = {}): Pool =>
  ({
    name: "web",
    target: "docker",
    runtime: "plain",
    image: "worker:1",
    capabilities: [],
    agents_per_instance: 1,
    desired_replicas: 0,
    max_replicas: 2,
    env: {},
    cpus: null,
    memory_mb: null,
    allow_mutable_images: false,
    docker: { host: null, network: null, extra_hosts: [], volumes: [], runtime: null },
    kubernetes: null,
    nanoclaw: null,
    template: null,
    status: "ok",
    last_error: null,
    live_instances: 0,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    version: "2026-01-01T00:00:00Z",
    system: null,
    ...over,
  }) as Pool;

const mock = vi.hoisted(() => ({
  list: [] as Pool[],
  deletes: [] as { name: string; confirm: string }[],
  failWith: undefined as Error | undefined,
}));

vi.mock("../live", async () => {
  const { useCallback, useEffect, useState } = await import("react");
  return {
    useLiveData: <T,>(load: () => Promise<T>) => {
      const [data, setData] = useState<T>();
      const reload = useCallback(() => load().then(setData), [load]);
      useEffect(() => {
        reload();
      }, []);
      return { data, error: undefined, reload };
    },
  };
});

vi.mock("../api", () => ({
  api: {
    GET: (path: string) => (path === "/api/pools" ? "pools" : "instances"),
    POST: () => "post",
    DELETE: (
      _: string,
      init: { params: { path: { name: string }; query: { confirm_name: string } } },
    ) => ({
      op: "delete",
      name: init.params.path.name,
      confirm: init.params.query.confirm_name,
    }),
  },
  call: async (req: unknown) => {
    if (req === "pools") return mock.list;
    if (req === "instances") return [];
    const r = req as { op?: string; name?: string; confirm?: string };
    if (r.op === "delete") {
      if (mock.failWith) throw mock.failWith;
      mock.deletes.push({ name: r.name ?? "", confirm: r.confirm ?? "" });
      mock.list = mock.list.filter((p) => p.name !== r.name);
      return undefined;
    }
    return undefined;
  },
}));

afterEach(() => cleanup());

beforeEach(() => {
  mock.list = [pool(), pool({ name: "orch", system: "orchestrator" })];
  mock.deletes = [];
  mock.failWith = undefined;
});

describe("Pools delete", () => {
  it("hides Delete on system pools and enables only when the name matches", async () => {
    render(<Pools />);
    expect(await screen.findByRole("button", { name: "Delete" })).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "Edit" })).toHaveLength(2);
    expect(screen.getAllByRole("button", { name: "Delete" })).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const confirm = screen.getByRole("button", { name: "Delete pool" });
    expect(confirm).toHaveProperty("disabled", true);
    fireEvent.change(screen.getByLabelText("Confirm pool name"), { target: { value: "we" } });
    expect(confirm).toHaveProperty("disabled", true);
    fireEvent.change(screen.getByLabelText("Confirm pool name"), { target: { value: "web" } });
    fireEvent.click(confirm);
    await waitFor(() => expect(mock.deletes).toEqual([{ name: "web", confirm: "web" }]));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Delete" })).toBeNull());
    expect(screen.getByText("Deleted “web”.")).toBeTruthy();
  });

  it("shows the server's live-instance conflict and keeps the pool", async () => {
    mock.failWith = new Error(
      "pool 'web' has 1 live instance(s); scale to zero and wait until they are terminated, then delete",
    );
    render(<Pools />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    fireEvent.change(screen.getByLabelText("Confirm pool name"), { target: { value: "web" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete pool" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/live instance/);
    expect(mock.deletes).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Delete pool" })).toHaveProperty("disabled", false);
  });
});
