// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TaskSteers } from "./TaskSteers";

type Item = { id: string; message: string; state: string; created_at: string };
const mock = vi.hoisted(() => ({ pages: {} as Record<string, Item[]> }));

vi.mock("../api", () => ({
  api: {
    GET: (
      path: string,
      opts: { params: { path: { task_id: string }; query: { attempt: number } } },
    ) => ({ path, key: `${opts.params.path.task_id}/${opts.params.query.attempt}` }),
  },
  call: async (req: { key: string }) => ({ items: mock.pages[req.key] ?? [] }),
}));
vi.mock("../live", async () => {
  const React = await import("react");
  return {
    useLiveData: (load: (signal: AbortSignal) => Promise<unknown>, deps: unknown[]) => {
      const [data, setData] = React.useState<unknown>();
      React.useEffect(() => {
        load(new AbortController().signal).then(setData);
      }, deps);
      return { data, error: undefined, reload: () => {} };
    },
  };
});
vi.mock("./Activity", () => ({ useNow: () => Date.now() }));
vi.mock("../DateChip", () => ({ DateChip: () => null }));

afterEach(cleanup);

const at = "2026-10-30T12:00:00Z";
const view = (taskId: string, attempt: number) => (
  <TaskSteers
    taskId={taskId}
    attempt={attempt}
    claimed={false}
    assigneeId={null}
    supportsSteer
    composerOpen={false}
    onComposerOpenChange={() => {}}
  />
);

describe("TaskSteers", () => {
  it("labels an unknown steer as possibly delivered, without Cancel", async () => {
    mock.pages = {
      "t1/1": [
        { id: "s1", message: "use the cache", state: "unknown", created_at: at },
        { id: "s2", message: "skip docs", state: "expired", created_at: at },
      ],
    };
    render(view("t1", 1));
    const row = (await screen.findByText("use the cache")).closest("li");
    expect(row?.textContent).toBe("unknown: may have been delivereduse the cache");
    expect(screen.getByText("skip docs").closest("li")?.textContent).toBe(
      "expiredskip docsexpired",
    );
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
  });

  it("shows only the steers of the attempt and task in view", async () => {
    mock.pages = {
      "t1/1": [{ id: "s1", message: "first attempt", state: "unknown", created_at: at }],
      "t1/2": [{ id: "s2", message: "second attempt", state: "delivered", created_at: at }],
      "t2/1": [],
    };
    const { rerender } = render(view("t1", 1));
    await screen.findByText("first attempt");
    rerender(view("t1", 2));
    await screen.findByText("second attempt");
    expect(screen.queryByText("first attempt")).toBeNull();
    rerender(view("t2", 1));
    await waitFor(() => expect(screen.queryByText("second attempt")).toBeNull());
    expect(screen.queryByText(/unknown: may have been delivered/)).toBeNull();
  });
});
