// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Agent } from "./api";
import styles from "./styles.css?raw";
import { WorkerPanelProvider } from "./WorkerPanel";
import { WorkersSidebar } from "./WorkersSidebar";

const calls = vi.hoisted(() => ({ paths: [] as string[] }));
const openWorker = vi.hoisted(() => vi.fn());

vi.mock("./api", () => ({
  api: { GET: (path: string) => path },
  call: async (path: string) => {
    calls.paths.push(path);
    if (path === "/api/agents") return [liveWorker];
    throw new Error(`unexpected ${path}`);
  },
}));

vi.mock("./live", async () => {
  const { useEffect, useState } = await import("react");
  return {
    useLiveData: <T,>(load: (signal: AbortSignal) => Promise<T>) => {
      const [data, setData] = useState<T>();
      const [error, setError] = useState<Error>();
      useEffect(() => {
        const controller = new AbortController();
        load(controller.signal).then(setData, setError);
        return () => controller.abort();
      }, []);
      return { data, error, reload: () => {} };
    },
  };
});

vi.mock("./views/Activity", () => ({
  useNow: () => Date.parse("2026-10-29T15:00:00Z"),
}));

const liveWorker: Agent = {
  id: "agent-w1",
  name: "nanoclaw-socket-abc12345",
  role: "worker",
  state: "active",
  capabilities: [],
  claimed_tasks: 1,
  last_heartbeat_at: "2026-10-29T14:59:50Z",
  quiet_since: null,
  ended_at: null,
  end_reason: null,
  runtime: { engine: "cursor" },
  instance_id: null,
  created_at: "2026-10-29T12:00:00Z",
};

function renderRail(selectedId: string | null = null) {
  return render(
    <WorkerPanelProvider selectedId={selectedId} openWorker={openWorker}>
      <WorkersSidebar selectedId={selectedId} />
    </WorkerPanelProvider>,
  );
}

afterEach(() => {
  cleanup();
  calls.paths = [];
  openWorker.mockClear();
});

describe("WorkersSidebar", () => {
  it("shows the worker rail without a right-rail live log block", async () => {
    renderRail();
    expect(await screen.findByRole("complementary", { name: "Running workers" })).toBeTruthy();
    expect(screen.getByText("Workers")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Open worker abc12345/i })).toBeTruthy();
    expect(screen.queryByText("Live log")).toBeNull();
    expect(document.querySelector(".rail-log")).toBeNull();
    expect(calls.paths.filter((p) => p === "/api/agents").length).toBeGreaterThan(0);
  });

  it("opens the worker panel from a rail chip", async () => {
    renderRail();
    fireEvent.click(await screen.findByRole("button", { name: /Open worker abc12345/i }));
    expect(openWorker).toHaveBeenCalledWith("agent-w1");
  });
});

describe("workers rail styles", () => {
  it("drops obsolete rail-log selectors from styles.css", () => {
    expect(styles).not.toMatch(/\.rail-log\b/);
    expect(styles).not.toMatch(/rail-log-head/);
  });
});
