// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Activity } from "./Activity";
import { AgentDetail } from "./AgentDetail";

const mock = vi.hoisted(() => ({
  agent: {} as Record<string, unknown>,
  models: undefined as undefined | { current: Record<string, unknown>; history: unknown[] },
  tasks: [] as unknown[],
  events: [] as unknown[],
  taskLog: { attempt: 2, lines: [] as unknown[], last_seq: 0 },
  fetchText: undefined as unknown as ReturnType<typeof vi.fn>,
  listener: undefined as undefined | ((e: unknown) => void),
}));

vi.mock("../api", () => ({
  api: { GET: (path: string, opts: unknown) => ({ path, opts }) },
  call: async ({ path }: { path: string }) => {
    if (path === "/api/agents/{agent_id}") return mock.agent;
    if (path === "/api/agents/{agent_id}/models")
      return mock.models ?? { current: {}, history: [] };
    if (path === "/api/tasks") return { items: mock.tasks };
    if (path === "/api/events") return mock.events;
    if (path === "/api/tasks/{task_id}/log") return mock.taskLog;
    if (path === "/api/tasks/{task_id}/steers") return { attempt: 2, items: [] };
    throw new Error(`unexpected ${path}`);
  },
  fetchText: (path: string) => mock.fetchText(path),
}));

vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  // Stable subscribe, like the real context value (TaskLog depends on that).
  const live = {
    subscribe: (fn: (e: unknown) => void) => {
      mock.listener = fn;
      return () => {
        mock.listener = undefined;
      };
    },
  };
  return {
    useLiveData: <T,>(load: () => Promise<T>) => {
      const [data, setData] = useState<T>();
      useEffect(() => {
        load().then(setData);
      }, []);
      return { data, error: undefined, reload: () => {} };
    },
    useLive: () => ({ ...live, connection: 1 }),
  };
});

const NOW = "2026-10-19T12:00:00Z";

const task = {
  id: "task-1",
  title: "Finance canary",
  state: "claimed",
  attempt: 2,
  progress_message: "running the tests",
  progress_percent: 40,
  progress_at: NOW,
  lease_expires_at: "2026-10-19T12:05:00Z",
} as const;

function note(id: number, attempt: number, message: string) {
  return { id, kind: "task.progress", subject_id: "task-1", at: NOW, data: { attempt, message } };
}

async function flush(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(NOW));
  mock.fetchText = vi.fn(async () => "claimed task-1\nsession started");
  mock.agent = {
    id: "agent-1",
    name: "nanoclaw-1",
    role: "worker",
    state: "active",
    last_heartbeat_at: NOW,
    capabilities: ["git"],
    instance_id: "inst-1",
    end_reason: null,
    runtime: { worker_version: "nanoclaw/1.2", engine: "codex", executor: null },
  };
  mock.tasks = [task];
  mock.events = [
    note(3, 2, "running the tests"),
    note(2, 2, "cloned the repo"),
    note(1, 1, "from the failed attempt"),
  ];
  mock.taskLog = {
    attempt: 2,
    lines: [{ seq: 1, source: "delegate", at: NOW, line: "thinking about the canary" }],
    last_seq: 1,
  };
  mock.models = undefined;
  mock.listener = undefined;
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("Activity", () => {
  it("shows the current attempt's notes only", () => {
    render(<Activity task={task} events={mock.events as never} />);
    expect(screen.getByText("running the tests")).toBeTruthy();
    expect(screen.getByText("cloned the repo")).toBeTruthy();
    expect(screen.queryByText("from the failed attempt")).toBeNull();
  });

  it("keeps counting how long the agent has been silent", async () => {
    const { container } = render(<Activity task={task} events={[]} />);
    const stamp = () => container.querySelector(".muted.small")?.textContent ?? "";
    expect(stamp()).toMatch(/updated now/);
    await flush(30_000);
    expect(stamp()).toMatch(/updated 30 seconds ago/);
  });

  it("is hidden for a finished task that sent no note", () => {
    const { container } = render(
      <Activity task={{ ...task, state: "succeeded", progress_message: null }} events={[]} />,
    );
    expect(container.innerHTML).toBe("");
  });
});

describe("AgentDetail", () => {
  it("shows the agent, its work and the task live log while holding a task", async () => {
    render(<AgentDetail agentId="agent-1" onClose={() => {}} />);
    await flush(); // agent + work
    await flush(); // TaskLog backfill
    expect(screen.getByText("nanoclaw/1.2 · codex · unknown")).toBeTruthy();
    expect(screen.getByText("Global - Finance canary")).toBeTruthy();
    expect(screen.getByText("running the tests")).toBeTruthy();
    expect(screen.getByText("40%")).toBeTruthy();
    expect(screen.getByLabelText("Live")).toBeTruthy();
    expect(screen.getByText("thinking about the canary")).toBeTruthy();
    expect(screen.getByText("No model reports.")).toBeTruthy();
    expect(mock.fetchText).not.toHaveBeenCalled();
  });

  it("shows current and recent models from the agent models endpoint", async () => {
    mock.models = {
      current: {
        harness: {
          engine: "codex",
          configured_model: "gpt-6",
          observed_model: null,
          evidence: "not_reported",
        },
      },
      history: [
        {
          id: "r1",
          layer: "delegate",
          executor: "claude",
          observed_model: "sonnet",
          state: "succeeded",
        },
      ],
    };
    render(<AgentDetail agentId="agent-1" onClose={() => {}} />);
    await flush();
    expect(screen.getByText(/codex · gpt-6/)).toBeTruthy();
    expect(screen.getByLabelText("Recent model runs").textContent).toMatch(
      /delegate · claude · sonnet · succeeded/,
    );
  });

  it("falls back to container output when the agent holds no task", async () => {
    mock.tasks = [];
    const { unmount } = render(<AgentDetail agentId="agent-1" onClose={() => {}} />);
    await flush();
    expect(screen.getByText("Holds no task.")).toBeTruthy();
    expect(screen.getByText(/session started/)).toBeTruthy();
    expect(mock.fetchText).toHaveBeenCalledWith("/api/instances/inst-1/logs?tail=200");

    await flush(4_000);
    expect(mock.fetchText).toHaveBeenCalledTimes(3); // t=0, 2s, 4s
    unmount();
    await flush(10_000);
    expect(mock.fetchText).toHaveBeenCalledTimes(3); // closing stops the reads
  });

  it("stops container reads while paused", async () => {
    mock.tasks = [];
    render(<AgentDetail agentId="agent-1" onClose={() => {}} />);
    await flush();
    act(() => screen.getByRole("button", { name: "Pause" }).click());
    await flush(20_000);
    expect(mock.fetchText).toHaveBeenCalledTimes(1);
  });

  it("says why there is no output for an agent outside a pool", async () => {
    mock.agent = { ...mock.agent, instance_id: null };
    mock.tasks = [];
    render(<AgentDetail agentId="agent-1" onClose={() => {}} />);
    await flush();
    expect(screen.getByText("Holds no task.")).toBeTruthy();
    expect(screen.getByText(/not started from a pool/)).toBeTruthy();
    expect(mock.fetchText).not.toHaveBeenCalled();
  });
});
