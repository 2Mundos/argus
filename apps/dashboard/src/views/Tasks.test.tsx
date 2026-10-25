// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Tasks } from "./Tasks";

const mock = vi.hoisted(() => ({ items: [] as unknown[] }));
vi.mock("../api", () => ({
  api: { GET: () => ({}), POST: () => ({}) },
  call: async () => ({}),
  newestTasks: async () => ({ items: mock.items, total: mock.items.length }),
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
    expect(await screen.findByText("codex · gpt-6")).toBeTruthy();
    expect(screen.getByText("claude · sonnet · configured")).toBeTruthy();
    const card = screen.getByRole("button", { name: /No model/ });
    expect(card.querySelector('[aria-label="Models used"]')).toBeNull();
  });
});
