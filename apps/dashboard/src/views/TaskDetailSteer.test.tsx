// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskDetail } from "./TaskDetail";

const mock = vi.hoisted(() => ({
  task: {} as Record<string, unknown>,
  features: ["steer"] as string[],
  posts: [] as { path: string; body: unknown }[],
  steerReads: 0,
  attempts: {} as Record<string, { attempt: number; steer_unknown?: number }[]>,
}));

vi.mock("../api", () => ({
  api: {
    GET: (path: string, opts?: { params?: { path?: { task_id?: string } } }) => ({
      method: "GET",
      path,
      taskId: opts?.params?.path?.task_id,
    }),
    POST: (path: string, opts: { body: unknown }) => ({ method: "POST", path, body: opts.body }),
  },
  call: async (req: { method: string; path: string; body?: unknown; taskId?: string }) => {
    if (req.method === "POST") {
      mock.posts.push({ path: req.path, body: req.body });
      return {};
    }
    if (req.path === "/api/tasks/{task_id}/steers") {
      mock.steerReads += 1;
      return { attempt: 2, items: [] };
    }
    if (req.path === "/api/agents/{agent_id}") return { runtime: { features: mock.features } };
    if (req.path === "/api/tasks/{task_id}") return { ...mock.task, id: req.taskId };
    if (req.path === "/api/tasks/{task_id}/models") return { executions: [], timeline: [] };
    if (req.path === "/api/tasks/{task_id}/delivery") return null;
    if (req.path === "/api/tasks/{task_id}/attempts")
      return mock.attempts[req.taskId ?? ""] ?? [{ attempt: 1 }, { attempt: 2 }];
    return [];
  },
  download: vi.fn(),
}));
vi.mock("../live", async () => {
  const React = await import("react");
  return {
    useLiveData: (load: (signal: AbortSignal) => Promise<unknown>, deps: unknown[]) => {
      const [data, setData] = React.useState<unknown>();
      const [error, setError] = React.useState<Error>();
      const [nonce, setNonce] = React.useState(0);
      React.useEffect(() => {
        load(new AbortController().signal).then(setData, setError);
      }, [...deps, nonce]);
      return { data, error, reload: () => setNonce((n) => n + 1) };
    },
  };
});
vi.mock("./Activity", () => ({ Activity: () => null, useNow: () => Date.now() }));
vi.mock("../WorkerChip", () => ({
  WorkerChip: () => null,
  WorkerActorChip: () => null,
}));
vi.mock("./EventList", () => ({ EventList: () => null }));
vi.mock("./TaskEditForm", () => ({ TaskEditForm: () => null }));
vi.mock("./TaskTerminal", () => ({ TaskTerminal: () => null }));
vi.mock("../delivery", () => ({ TaskDelivery: () => null }));
vi.mock("../failover", () => ({ TaskFailover: () => null }));

const base = {
  id: "t1",
  title: "steer task",
  project: "global",
  state: "claimed",
  kind: "generic",
  difficulty: "standard",
  difficulty_source: "heuristic",
  knowledge: [],
  attempt: 2,
  failures: 1,
  max_attempts: 3,
  required_capabilities: [],
  depends_on: [],
  created_at: "2026-10-20T12:00:00Z",
  created_by: "test",
  editable_fields: [],
  spec: "",
  result: null,
  assignee_agent_id: "agent-1",
};

beforeEach(() => {
  mock.task = base;
  mock.features = ["steer"];
  mock.posts = [];
  mock.steerReads = 0;
  mock.attempts = {};
});
afterEach(cleanup);

const steerButton = () => screen.queryByRole("button", { name: "Steer" });

async function openSteerComposer() {
  const btn = await screen.findByRole("button", { name: /^(Steer|Close steer)$/ });
  // Retry without toggling shut: assignee / attempt effects can lag the first click.
  await waitFor(() => {
    if (btn.getAttribute("aria-expanded") !== "true") fireEvent.click(btn);
    expect(screen.getByPlaceholderText("Steer this attempt…")).toBeTruthy();
  });
}

describe("TaskDetail actions-row steer", () => {
  it("opens the composer for a claimed task on the current attempt", async () => {
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    await openSteerComposer();
  });

  it("sends the trimmed message and reloads the list", async () => {
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    await openSteerComposer();
    const box = screen.getByPlaceholderText("Steer this attempt…");
    await waitFor(() => expect(mock.steerReads).toBeGreaterThan(0));
    const reads = mock.steerReads;
    fireEvent.change(box, { target: { value: "  use the cache  " } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(mock.posts).toHaveLength(1));
    expect(mock.posts[0]).toEqual({
      path: "/api/tasks/{task_id}/steers",
      body: { message: "use the cache" },
    });
    await waitFor(() => expect(mock.steerReads).toBeGreaterThan(reads));
    expect(screen.queryByPlaceholderText("Steer this attempt…")).toBeNull();
  });

  it("closes on Escape", async () => {
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    await openSteerComposer();
    fireEvent.keyDown(screen.getByPlaceholderText("Steer this attempt…"), {
      key: "Escape",
    });
    expect(screen.queryByPlaceholderText("Steer this attempt…")).toBeNull();
  });

  it.each(["running", "queued", "failed", "succeeded"])("is hidden while %s", async (state) => {
    mock.task = { ...base, state };
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    await screen.findByText("Attempt");
    expect(steerButton()).toBeNull();
  });

  it("is hidden for attempt 0", async () => {
    mock.task = { ...base, attempt: 0 };
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    await screen.findByText("Attempt");
    expect(steerButton()).toBeNull();
  });

  it("is hidden when the assignee lacks the steer feature", async () => {
    mock.features = [];
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    await screen.findByText("Attempt");
    await waitFor(() => expect(steerButton()).toBeNull());
  });
});

describe("TaskDetail unknown steers", () => {
  const note = () => screen.queryByRole("note");

  it("follows the selected attempt", async () => {
    mock.attempts.t1 = [
      { attempt: 1, steer_unknown: 2 },
      { attempt: 2, steer_unknown: 0 },
    ];
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    await screen.findByRole("button", { name: "Attempt 2 (current)" });
    expect(note()).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Attempt 1" }));
    expect((await screen.findByRole("note")).textContent).toBe(
      "2 steers to attempt 1 unknown: may have been delivered (never acknowledged)",
    );
    fireEvent.click(screen.getByRole("button", { name: "Attempt 2 (current)" }));
    await waitFor(() => expect(note()).toBeNull());
  });

  it("does not carry over to another task", async () => {
    mock.attempts.t1 = [
      { attempt: 1, steer_unknown: 0 },
      { attempt: 2, steer_unknown: 1 },
    ];
    mock.attempts.t2 = [{ attempt: 1 }, { attempt: 2 }];
    const { rerender } = render(<TaskDetail taskId="t1" onClose={() => {}} />);
    expect((await screen.findByRole("note")).textContent).toContain("1 steer to attempt 2");
    rerender(<TaskDetail taskId="t2" onClose={() => {}} />);
    await waitFor(() => expect(note()).toBeNull());
  });
});
