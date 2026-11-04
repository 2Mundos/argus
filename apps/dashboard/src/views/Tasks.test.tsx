// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Tasks } from "./Tasks";

const mock = vi.hoisted(() => ({ items: [] as unknown[], loading: false }));
vi.mock("../api", () => ({
  api: { GET: () => ({}), POST: () => ({}) },
  call: async () => ({}),
  ENDED_STATES: ["succeeded", "failed", "cancelled"],
  boardTasks: async () => ({ items: mock.items, total: mock.items.length, loading: mock.loading }),
}));
vi.mock("../live", async () => {
  const React = await import("react");
  return {
    useLiveData: (load: () => Promise<unknown>) => {
      const [data, setData] = React.useState<unknown>();
      React.useEffect(() => {
        load().then(setData);
      }, []);
      return { data, error: undefined };
    },
  };
});
vi.mock("./TaskDetail", () => ({ TaskDetail: () => null }));
vi.mock("./ArchiveTasks", () => ({ ArchiveTasks: () => null }));

afterEach(() => {
  cleanup();
  mock.items = [];
  mock.loading = false;
});

describe("Tasks model chips", () => {
  it("shows observed and configured models only on tasks with model usage", async () => {
    mock.items = [
      {
        id: "1",
        title: "Observed",
        project: "global",
        state: "ready",
        kind: "generic",
        priority: 0,
        attempt: 1,
        models: { used: [{ layer: "harness", tool: "codex", model: "gpt-6", observed: true }] },
      },
      {
        id: "2",
        title: "Configured",
        project: "global",
        state: "ready",
        kind: "generic",
        priority: 0,
        attempt: 1,
        models: { used: [{ layer: "delegate", tool: "claude", model: "sonnet", observed: false }] },
      },
      {
        id: "3",
        title: "No model",
        project: "global",
        state: "ready",
        kind: "generic",
        priority: 0,
        attempt: 1,
        models: { used: [] },
      },
    ];
    render(<Tasks />);
    expect(await screen.findByText("gpt-6")).toBeTruthy();
    expect(screen.getByTitle("codex · gpt-6")).toBeTruthy();
    expect(screen.getByText("sonnet")).toBeTruthy();
    expect(screen.getByTitle("claude · sonnet · configured")).toBeTruthy();
    expect(screen.getByLabelText("configured")).toBeTruthy();
    expect(screen.getByLabelText("OpenAI")).toBeTruthy();
    expect(screen.getByLabelText("Anthropic")).toBeTruthy();
    const card = screen.getByRole("button", { name: /No model/ });
    expect(card.querySelector('[aria-label="Models used"]')).toBeNull();
  });
});

describe("Tasks board while the ended tasks load", () => {
  it("shows ended columns as unknown, not empty, and hides the totals notice", async () => {
    mock.loading = true;
    mock.items = [
      {
        id: "1",
        title: "Active",
        project: "global",
        state: "ready",
        kind: "generic",
        priority: 0,
        attempt: 1,
      },
    ];
    render(<Tasks />);
    expect(await screen.findByRole("button", { name: /Active/ })).toBeTruthy();
    expect(screen.getByLabelText("Done: …")).toBeTruthy();
    expect(screen.getByLabelText("Failed / cancelled: …")).toBeTruthy();
    // Active columns are real: an empty one is a plain zero.
    expect(screen.getByLabelText("Running: 0")).toBeTruthy();
    expect(screen.queryByText(/Showing the newest/)).toBeNull();
  });
});
