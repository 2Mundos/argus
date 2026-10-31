// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Agent } from "./api";
import styles from "./styles.css?raw";
import { WorkerPanelProvider } from "./WorkerPanel";
import { WorkersSidebar } from "./WorkersSidebar";

const calls = vi.hoisted(() => ({
  paths: [] as string[],
  agents: undefined as Agent[] | undefined,
}));
const openWorker = vi.hoisted(() => vi.fn());

vi.mock("./api", () => ({
  api: { GET: (path: string) => path },
  call: async (path: string) => {
    calls.paths.push(path);
    if (path === "/api/agents") return calls.agents ?? [liveWorker];
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
  calls.agents = undefined;
  openWorker.mockClear();
  localStorage.clear();
});

describe("WorkersSidebar", () => {
  it("shows the worker rail without a right-rail live log block", async () => {
    renderRail();
    expect(await screen.findByRole("complementary", { name: "Running workers" })).toBeTruthy();
    expect(screen.getByText("Workers")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /Open worker nanoclaw-socket-abc12345 \(running\)/i }),
    ).toBeTruthy();
    expect(screen.queryByText("Live log")).toBeNull();
    expect(document.querySelector(".rail-log")).toBeNull();
    expect(calls.paths.filter((p) => p === "/api/agents").length).toBeGreaterThan(0);
  });

  it("collapses the rail and remembers it", async () => {
    renderRail();
    fireEvent.click(await screen.findByRole("button", { name: "Collapse workers" }));
    const icon = screen.getByRole("button", { name: "Open worker nanoclaw-socket-abc12345" });
    expect(icon.getAttribute("title")).toBe("nanoclaw-socket-abc12345");
    expect(screen.queryByText("1")).toBeNull();
    expect(localStorage.getItem("argus.workers-rail")).toBe("collapsed");

    cleanup();
    renderRail();
    expect(
      await screen.findByRole("button", { name: "Open worker nanoclaw-socket-abc12345" }),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Expand workers" }));
    expect(
      await screen.findByRole("button", {
        name: /Open worker nanoclaw-socket-abc12345 \(running\)/i,
      }),
    ).toBeTruthy();
    expect(localStorage.getItem("argus.workers-rail")).toBe("open");
  });

  it("opens the worker panel from a rail chip", async () => {
    renderRail();
    fireEvent.click(
      await screen.findByRole("button", {
        name: /Open worker nanoclaw-socket-abc12345 \(running\)/i,
      }),
    );
    expect(openWorker).toHaveBeenCalledWith("agent-w1");
  });
});

describe("WorkersSidebar quiet and ended workers", () => {
  it("shows state quiet with the age of quiet_since, not the heartbeat age", async () => {
    calls.agents = [{ ...liveWorker, quiet_since: "2026-10-29T14:35:00Z" }];
    renderRail();
    const chip = await screen.findByRole("button", {
      name: /Open worker nanoclaw-socket-abc12345 \(quiet\)/i,
    });
    expect(chip.getAttribute("title")).toContain("quiet");
    expect(chip.querySelector(".worker-age")?.textContent).toBe("25m");
    expect(chip.textContent).not.toContain("10s");
  });

  it("lists at most eight ended workers, newest first, dimmed with end_reason", async () => {
    const ended = (n: number): Agent => ({
      ...liveWorker,
      id: `agent-e${n}`,
      name: `ended-${n}`,
      state: "lost",
      claimed_tasks: 0,
      ended_at: `2026-10-29T${String(n).padStart(2, "0")}:00:00Z`,
      end_reason: `reason-${n}`,
    });
    calls.agents = [liveWorker, ...[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(ended)];
    renderRail();
    await screen.findByRole("button", { name: /Open worker ended-10/i });
    const chips = [...document.querySelectorAll(".worker-chip.worker-ended")];
    expect(chips.map((c) => c.querySelector(".worker-name")?.textContent)).toEqual([
      "ended-10",
      "ended-9",
      "ended-8",
      "ended-7",
      "ended-6",
      "ended-5",
      "ended-4",
      "ended-3",
    ]);
    expect(chips[0]?.querySelector(".worker-end-reason")?.textContent).toBe("reason-10");
    expect(screen.queryByText("reason-2")).toBeNull();
  });

  it("omits the end reason when it is null", async () => {
    calls.agents = [
      { ...liveWorker, state: "gone", ended_at: "2026-10-29T14:00:00Z", end_reason: null },
    ];
    renderRail();
    await screen.findByRole("button", { name: /Open worker nanoclaw-socket-abc12345/i });
    expect(document.querySelector(".worker-end-reason")).toBeNull();
  });
});

describe("workers rail styles", () => {
  it("drops obsolete rail-log selectors from styles.css", () => {
    expect(styles).not.toMatch(/\.rail-log\b/);
    expect(styles).not.toMatch(/rail-log-head/);
  });
});
