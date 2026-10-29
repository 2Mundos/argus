// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskChanges } from "./TaskChanges";

const mock = vi.hoisted(() => ({
  answers: new Map<number, unknown>(), // attempt → snapshot; missing → 404
  queries: [] as { attempt?: number }[],
  listener: undefined as undefined | ((e: unknown) => void),
}));

vi.mock("../api", () => {
  class ApiError extends Error {
    constructor(
      readonly status: number,
      readonly code: string,
    ) {
      super(code);
    }
  }
  return {
    ApiError,
    api: {
      GET: (_path: string, opts: { params: { query: { attempt?: number } } }) => {
        mock.queries.push(opts.params.query);
        return opts.params.query.attempt;
      },
    },
    call: async (attempt: number) => {
      const got = mock.answers.get(attempt);
      if (!got) throw new ApiError(404, "no_changes");
      return got;
    },
  };
});

vi.mock("../live", () => {
  const live = {
    subscribe: (fn: (e: unknown) => void) => {
      mock.listener = fn;
      return () => {
        mock.listener = undefined;
      };
    },
  };
  return { useLive: () => ({ ...live, connection: 1 }) };
});

const SHA = "0123456789abcdef0123456789abcdef01234567";
const snapshot = (over: Record<string, unknown> = {}) => ({
  task_id: "t1",
  attempt: 2,
  at: new Date().toISOString(),
  received_at: new Date().toISOString(),
  branch: "argus/task-t1",
  base: { ref: "origin/main", sha: SHA },
  head: SHA,
  commits: [{ sha: SHA, subject: "feat: add x" }],
  files: [{ path: "x.py", status: "M", added: 3, deleted: 1, binary: false }],
  untracked: [{ path: "notes.txt", bytes: 4 }],
  patch: "diff --git a/x.py b/x.py\n@@ -1 +1 @@\n-<b>old</b>\n+new\n",
  truncated: { files: false, patch: false },
  ...over,
});

beforeEach(() => {
  mock.answers = new Map();
  mock.queries = [];
});
afterEach(cleanup);

describe("TaskChanges", () => {
  it("shows the files, commits and patch of the current attempt, as text", async () => {
    mock.answers.set(2, snapshot());
    render(<TaskChanges taskId="t1" attempt={2} />);
    await screen.findByText("feat: add x");
    expect(mock.queries[0]).toEqual({ attempt: 2 });
    expect(screen.getByText(/argus\/task-t1 @ 01234567/)).toBeTruthy();
    expect(screen.getByText("untracked, not in the diff")).toBeTruthy();
    expect(screen.getByText("-<b>old</b>").className).toBe("diff-del"); // never HTML
    expect(screen.getByText("+new").className).toBe("diff-add");
  });

  it("says when nothing was reported, and refetches on its own task.changes event", async () => {
    render(<TaskChanges taskId="t1" attempt={2} />);
    await screen.findByText(/No changes reported/);
    mock.answers.set(2, snapshot());
    act(() => mock.listener?.({ kind: "task.changes", data: { task_id: "other", attempt: 2 } }));
    act(() => mock.listener?.({ kind: "task.changes", data: { task_id: "t1", attempt: 1 } }));
    expect(mock.queries).toHaveLength(1);
    act(() => mock.listener?.({ kind: "task.changes", data: { task_id: "t1", attempt: 2 } }));
    await screen.findByText("feat: add x");
  });

  it("renders nothing until a snapshot exists when asked to", async () => {
    const { container } = render(<TaskChanges taskId="t1" attempt={2} hideWhenEmpty />);
    await waitFor(() => expect(mock.queries).toHaveLength(1));
    await waitFor(() => expect(container.innerHTML).toBe(""));
  });

  it("switches to an earlier attempt and flags a cut snapshot", async () => {
    mock.answers.set(3, snapshot({ attempt: 3 }));
    mock.answers.set(
      2,
      snapshot({
        commits: [{ sha: SHA, subject: "older" }],
        truncated: { files: false, patch: true },
      }),
    );
    render(<TaskChanges taskId="t1" attempt={3} />);
    await screen.findByText("feat: add x");
    fireEvent.change(screen.getByLabelText("Attempt"), { target: { value: "2" } });
    await screen.findByText("older");
    expect(screen.getByText(/the patch is incomplete/)).toBeTruthy();
  });
});
