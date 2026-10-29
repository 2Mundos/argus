// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskDetail } from "./TaskDetail";

const mock = vi.hoisted(() => ({
  attempts: [] as number[],
  logs: new Map<number, { seq: number; source: string; line: string }[]>(),
  changes: new Map<number, string>(), // attempt → commit subject; missing → 404
  notes: [] as unknown[],
  logQueries: [] as number[],
}));

vi.mock("../api", () => {
  class ApiError extends Error {
    constructor(readonly status: number) {
      super("no_changes");
    }
  }
  return {
    ApiError,
    api: {
      GET: (path: string, opts?: { params: { query?: { attempt?: number } } }) => ({
        path,
        attempt: opts?.params.query?.attempt,
      }),
    },
    call: async ({ path, attempt }: { path: string; attempt?: number }) => {
      if (path.endsWith("/attempts")) return mock.attempts.map((attempt) => ({ attempt }));
      if (path.endsWith("/log")) {
        mock.logQueries.push(attempt as number);
        const lines = mock.logs.get(attempt as number) ?? [];
        return { lines, last_seq: lines.at(-1)?.seq ?? 0 };
      }
      if (path.endsWith("/changes")) {
        const subject = mock.changes.get(attempt as number);
        if (!subject) throw new ApiError(404);
        return {
          task_id: "t1",
          attempt,
          at: new Date().toISOString(),
          branch: "b",
          base: null,
          head: null,
          commits: [{ sha: `${attempt}`.padEnd(40, "0"), subject }],
          files: [],
          untracked: [],
          patch: "",
          truncated: { files: false, patch: false },
        };
      }
      if (path.endsWith("/models")) return { executions: [], selections: [] };
      if (path.endsWith("/artifacts") || path.endsWith("/events"))
        return path.endsWith("/events") ? mock.notes : [];
      if (path.endsWith("/delivery")) return null;
      return {
        id: "t1",
        title: "t",
        project: "p",
        state: "failed",
        attempt: 2,
        failures: 1,
        max_attempts: 3,
        required_capabilities: ["repository:write"],
        depends_on: [],
        knowledge: [],
        created_at: "2026-10-20T12:00:00Z",
        editable_fields: [],
        spec: "",
        result: null,
        assignee_agent_id: null,
        lease_expires_at: null,
      };
    },
    download: vi.fn(),
  };
});
vi.mock("../live", async () => {
  const React = await import("react");
  const live = { subscribe: () => () => {}, connection: 1 };
  return {
    useLive: () => live,
    useLiveData: (load: () => Promise<unknown>) => {
      const [data, setData] = React.useState<unknown>();
      React.useEffect(() => {
        load().then(setData);
      }, []);
      return { data, reload: () => {} };
    },
  };
});
vi.mock("./AgentDetail", () => ({ AgentDetail: () => null }));
vi.mock("./EventList", () => ({ EventList: () => null }));
vi.mock("./TaskEditForm", () => ({ TaskEditForm: () => null }));
vi.mock("./TaskSteers", () => ({ TaskSteers: () => null }));
vi.mock("../delivery", () => ({ TaskDelivery: () => null }));
vi.mock("../failover", () => ({ TaskFailover: () => null }));

const line = (seq: number, text: string) => ({ seq, source: "worker", line: text });

beforeEach(() => {
  mock.attempts = [1, 2];
  mock.logs = new Map([
    [1, [line(3, "first run output")]],
    [2, [line(9, "second run output")]],
  ]);
  mock.changes = new Map([
    [1, "attempt one commit"],
    [2, "attempt two commit"],
  ]);
  mock.notes = [];
  mock.logQueries = [];
});
afterEach(cleanup);

describe("attempt strip", () => {
  it("defaults to the current attempt and switches Live and Changes together", async () => {
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    await screen.findByText("second run output");
    await screen.findByText("attempt two commit");
    fireEvent.click(screen.getByRole("button", { name: "Attempt 1" }));
    await screen.findByText("first run output");
    await screen.findByText("attempt one commit");
    // never mixed: nothing of attempt 2 is left on screen
    expect(screen.queryByText("second run output")).toBeNull();
    expect(screen.queryByText("attempt two commit")).toBeNull();
    expect(mock.logQueries.every((a) => a === 1 || a === 2)).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Attempt 2 (current)" }));
    await screen.findByText("second run output");
    expect(screen.queryByText("first run output")).toBeNull();
  });

  it("shows explicit empty states for an attempt with no rows or snapshot", async () => {
    mock.logs.delete(1);
    mock.changes.delete(1);
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    await screen.findByText("second run output");
    fireEvent.click(screen.getByRole("button", { name: "Attempt 1" }));
    await screen.findByText("No output yet.");
    await screen.findByText(/No changes reported for this attempt yet/);
    expect(screen.queryByText("second run output")).toBeNull();
    expect(screen.queryByText("attempt two commit")).toBeNull();
    expect(screen.getByText("No progress notes for this attempt.")).toBeTruthy();
  });

  it("scopes progress notes to the selected attempt", async () => {
    const note = (id: string, attempt: number, message: string) => ({
      id,
      kind: "task.progress",
      subject_id: "t1",
      at: new Date().toISOString(),
      data: { attempt, message },
    });
    mock.notes = [note("e2", 2, "note from two"), note("e1", 1, "note from one")];
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    await screen.findByText("second run output");
    expect(screen.queryByText("note from one")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Attempt 1" }));
    await screen.findByText("note from one");
    expect(screen.queryByText("note from two")).toBeNull();
  });

  it("renders no strip for a single attempt", async () => {
    mock.attempts = [2];
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    await screen.findByText("second run output");
    await waitFor(() => expect(screen.queryByRole("group", { name: "Attempts" })).toBeNull());
  });
});
