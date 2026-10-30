// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TaskSteers } from "./TaskSteers";

type Item = { id: string; message: string; state: string; created_at: string };
type Post = { taskId: string; message: string; resolve: () => void };
const mock = vi.hoisted(() => ({
  pages: {} as Record<string, Item[]>,
  posts: [] as Post[],
}));

vi.mock("../api", () => ({
  api: {
    GET: (
      path: string,
      opts: { params: { path: { task_id: string }; query: { attempt: number } } },
    ) => ({ path, key: `${opts.params.path.task_id}/${opts.params.query.attempt}` }),
    POST: (
      _path: string,
      opts: { params: { path: { task_id: string } }; body: { message: string } },
    ) => ({ post: { taskId: opts.params.path.task_id, message: opts.body.message } }),
  },
  call: (req: { key?: string; post?: { taskId: string; message: string } }) => {
    const post = req.post;
    if (post)
      return new Promise<void>((resolve) => {
        mock.posts.push({ ...post, resolve });
      });
    return Promise.resolve({ items: mock.pages[req.key ?? ""] ?? [] });
  },
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

afterEach(() => {
  cleanup();
  mock.pages = {};
  mock.posts = [];
});

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

  describe("composer", () => {
    // The callers own the open state; the composer opens from their Steer button.
    function Host({ taskId, attempt, claimed = true }: Spot) {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            Steer
          </button>
          <TaskSteers
            taskId={taskId}
            attempt={attempt}
            claimed={claimed}
            assigneeId="w1"
            supportsSteer
            composerOpen={open}
            onComposerOpenChange={setOpen}
          />
        </>
      );
    }
    type Spot = { taskId: string; attempt: number; claimed?: boolean };
    const box = () => screen.queryByRole("textbox", { name: "Steer this attempt…" });
    const openAndType = (text: string) => {
      fireEvent.click(screen.getByRole("button", { name: "Steer" }));
      fireEvent.change(box() as HTMLTextAreaElement, { target: { value: text } });
    };

    it("keeps the exact draft open across a live refetch of the same attempt", () => {
      const { rerender } = render(<Host taskId="t1" attempt={1} />);
      openAndType("  use the cache\n");
      rerender(<Host taskId="t1" attempt={1} />);
      expect((box() as HTMLTextAreaElement).value).toBe("  use the cache\n");
    });

    it("closes and drops the draft on another task or a new attempt", async () => {
      const { rerender } = render(<Host taskId="t1" attempt={1} />);
      openAndType("for t1");
      rerender(<Host taskId="t2" attempt={1} />);
      await waitFor(() => expect(box()).toBeNull());
      fireEvent.click(screen.getByRole("button", { name: "Steer" }));
      expect((box() as HTMLTextAreaElement).value).toBe("");

      fireEvent.change(box() as HTMLTextAreaElement, { target: { value: "for attempt 1" } });
      rerender(<Host taskId="t2" attempt={2} />);
      await waitFor(() => expect(box()).toBeNull());
      fireEvent.click(screen.getByRole("button", { name: "Steer" }));
      expect((box() as HTMLTextAreaElement).value).toBe("");
      expect(mock.posts).toEqual([]);
    });

    it("lets a slow send for one task land without touching the next task's composer", async () => {
      const { rerender } = render(<Host taskId="t1" attempt={1} />);
      openAndType("for t1");
      fireEvent.click(screen.getByRole("button", { name: "Send steer" }));
      expect(mock.posts.map((p) => [p.taskId, p.message])).toEqual([["t1", "for t1"]]);

      rerender(<Host taskId="t2" attempt={1} />);
      await waitFor(() => expect(box()).toBeNull());
      openAndType("for t2");
      await act(async () => mock.posts[0]?.resolve());

      expect((box() as HTMLTextAreaElement).value).toBe("for t2");
      expect((box() as HTMLTextAreaElement).disabled).toBe(false);
      fireEvent.click(screen.getByRole("button", { name: "Send steer" }));
      expect(mock.posts.map((p) => [p.taskId, p.message])).toEqual([
        ["t1", "for t1"],
        ["t2", "for t2"],
      ]);
    });

    it("cannot submit while browsing an attempt that is not the claimed one", () => {
      render(<Host taskId="t1" attempt={1} claimed={false} />);
      fireEvent.click(screen.getByRole("button", { name: "Steer" }));
      expect(box()).toBeNull();
      expect(screen.queryByRole("button", { name: "Send steer" })).toBeNull();
      expect(mock.posts).toEqual([]);
    });
  });
});
