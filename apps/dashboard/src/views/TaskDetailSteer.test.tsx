// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskDetail } from "./TaskDetail";

const mock = vi.hoisted(() => ({
  task: {} as Record<string, unknown>,
  features: ["steer"] as string[],
  posts: [] as { path: string; body: unknown }[],
  steerReads: 0,
}));

vi.mock("../api", () => ({
  api: {
    GET: (path: string) => ({ method: "GET", path }),
    POST: (path: string, opts: { body: unknown }) => ({ method: "POST", path, body: opts.body }),
  },
  call: async (req: { method: string; path: string; body?: unknown }) => {
    if (req.method === "POST") {
      mock.posts.push({ path: req.path, body: req.body });
      return {};
    }
    if (req.path === "/api/tasks/{task_id}/steers") {
      mock.steerReads += 1;
      return { attempt: 2, items: [] };
    }
    if (req.path === "/api/agents/{agent_id}") return { runtime: { features: mock.features } };
    if (req.path === "/api/tasks/{task_id}") return mock.task;
    if (req.path === "/api/tasks/{task_id}/models") return { executions: [], timeline: [] };
    if (req.path === "/api/tasks/{task_id}/delivery") return null;
    if (req.path === "/api/tasks/{task_id}/attempts") return [{ attempt: 1 }, { attempt: 2 }];
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
vi.mock("./TaskLog", () => ({ TaskLog: () => null }));
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
});
afterEach(cleanup);

const steerButton = () => screen.queryByRole("button", { name: "Steer" });

describe("TaskDetail actions-row steer", () => {
  it("opens the composer for a claimed task on the current attempt", async () => {
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "Steer" }));
    expect(await screen.findByPlaceholderText("Steer this attempt…")).toBeTruthy();
  });

  it("sends the trimmed message and reloads the list", async () => {
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "Steer" }));
    const box = await screen.findByPlaceholderText("Steer this attempt…");
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
    fireEvent.click(await screen.findByRole("button", { name: "Steer" }));
    fireEvent.keyDown(await screen.findByPlaceholderText("Steer this attempt…"), {
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
