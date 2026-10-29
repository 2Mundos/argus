// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ArgusEvent } from "../api";
import { LiveStatusBar } from "./LiveStatusBar";

type Listener = (event: ArgusEvent) => void;

const mock = vi.hoisted(() => {
  const listeners = new Set<Listener>();
  return {
    history: [] as ArgusEvent[],
    listeners,
    status: "live" as "live" | "connecting" | "offline",
    // Stable identity so LiveStatusBar's effect does not re-run every render.
    subscribe: (listener: Listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
});

vi.mock("../api", () => ({
  api: { GET: (path: string) => path },
  call: async (path: string) => {
    if (path === "/api/events") return mock.history;
    throw new Error(`unexpected ${path}`);
  },
}));

vi.mock("../live", () => ({
  useLive: () => ({
    status: mock.status,
    version: 0,
    connection: 0,
    subscribe: mock.subscribe,
  }),
}));

vi.mock("./Activity", () => ({
  useNow: () => Date.parse("2026-10-29T15:00:00Z"),
}));

function event(over: Partial<ArgusEvent> & Pick<ArgusEvent, "id" | "kind">): ArgusEvent {
  return {
    at: "2026-10-29T14:59:00Z",
    actor: "agent:w1",
    subject_type: "task",
    subject_id: "task-abcd1234",
    data: {},
    ...over,
  };
}

function push(e: ArgusEvent) {
  for (const listener of mock.listeners) listener(e);
}

afterEach(() => {
  cleanup();
  mock.history = [];
  mock.listeners.clear();
  mock.status = "live";
});

describe("LiveStatusBar", () => {
  it("seeds from history and expands on click", async () => {
    mock.history = [event({ id: 1, kind: "task.claim", data: { from: "ready", to: "claimed" } })];
    render(<LiveStatusBar />);
    const strip = await screen.findByRole("button", { name: "Live status" });
    expect(strip.getAttribute("aria-expanded")).toBe("false");
    expect(strip.textContent).toMatch(/task\.claim/);
    expect(strip.textContent).toMatch(/ready → claimed/);
    expect(strip.textContent).toMatch(/live/);

    fireEvent.click(strip);
    expect(strip.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("region", { name: "Recent events" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "All events" }).getAttribute("href")).toBe("#events");
    expect(document.querySelector(".live-status.open")).toBeTruthy();
  });

  it("collapses on Escape", async () => {
    mock.history = [event({ id: 10, kind: "task.created", data: { to: "pending" } })];
    render(<LiveStatusBar />);
    const strip = await screen.findByRole("button", { name: "Live status" });
    fireEvent.click(strip);
    expect(strip.getAttribute("aria-expanded")).toBe("true");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(strip.getAttribute("aria-expanded")).toBe("false");
  });

  it("ignores task.log when seeding and when live", async () => {
    mock.history = [
      event({ id: 2, kind: "task.log", data: { lines: ["noise"] } }),
      event({ id: 3, kind: "task.progress", data: { message: "running tests" } }),
    ];
    render(<LiveStatusBar />);
    const strip = await screen.findByRole("button", { name: "Live status" });
    expect(strip.textContent).toMatch(/running tests/);
    expect(strip.textContent).not.toMatch(/noise/);

    await act(() => {
      push(event({ id: 4, kind: "task.log", data: { lines: ["more noise"] } }));
    });
    expect(strip.textContent).toMatch(/running tests/);
    expect(screen.queryByText(/more noise/)).toBeNull();
  });

  it("updates when a non-transient event arrives on the stream", async () => {
    mock.history = [event({ id: 5, kind: "task.created", data: { to: "pending" } })];
    render(<LiveStatusBar />);
    const strip = await screen.findByRole("button", { name: "Live status" });
    expect(strip.textContent).toMatch(/task\.created/);

    await act(() => {
      push(
        event({
          id: 6,
          kind: "agent.heartbeat",
          subject_type: "agent",
          subject_id: "agent-xyz",
          data: { reason: "lease renewed" },
        }),
      );
    });
    expect(strip.textContent).toMatch(/agent\.heartbeat/);
    expect(strip.textContent).toMatch(/lease renewed/);
    expect(strip.textContent).not.toMatch(/task\.created/);
  });

  it("keeps earlier events in the expanded feed", async () => {
    mock.history = [
      event({ id: 7, kind: "task.complete", data: { to: "done" } }),
      event({ id: 8, kind: "task.created", data: { to: "pending" } }),
    ];
    render(<LiveStatusBar />);
    const strip = await screen.findByRole("button", { name: "Live status" });
    fireEvent.click(strip);
    const feed = screen.getByRole("region", { name: "Recent events" });
    expect(feed.textContent).toMatch(/task\.complete/);
    expect(feed.textContent).toMatch(/task\.created/);
  });

  it("shows a quiet placeholder when nothing has arrived", async () => {
    mock.history = [];
    render(<LiveStatusBar />);
    expect(await screen.findByText("Waiting for events…")).toBeTruthy();
  });
});
