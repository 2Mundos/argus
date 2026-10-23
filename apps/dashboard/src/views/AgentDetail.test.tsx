// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Activity } from "./Activity";
import { AgentDetail } from "./AgentDetail";

const mock = vi.hoisted(() => ({
  agent: {} as Record<string, unknown>,
  tasks: [] as unknown[],
  events: [] as unknown[],
  fetchText: undefined as unknown as ReturnType<typeof vi.fn>,
}));

vi.mock("../api", () => ({
  api: { GET: (path: string, opts: unknown) => ({ path, opts }) },
  call: async ({ path }: { path: string }) => {
    if (path === "/api/agents/{agent_id}") return mock.agent;
    if (path === "/api/tasks") return { items: mock.tasks };
    if (path === "/api/events") return mock.events;
    throw new Error(`unexpected ${path}`);
  },
  fetchText: (path: string) => mock.fetchText(path),
}));

vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  return {
    useLiveData: <T,>(load: () => Promise<T>) => {
      const [data, setData] = useState<T>();
      useEffect(() => {
        load().then(setData);
      }, []);
      return { data, error: undefined, reload: () => {} };
    },
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
    render(<Activity task={task} events={[]} />);
    expect(screen.getByText(/updated now/)).toBeTruthy();
    await flush(30_000);
    expect(screen.getByText(/updated 30 seconds ago/)).toBeTruthy();
  });

  it("is hidden for a finished task that sent no note", () => {
    const { container } = render(
      <Activity task={{ ...task, state: "succeeded", progress_message: null }} events={[]} />,
    );
    expect(container.innerHTML).toBe("");
  });
});

describe("AgentDetail", () => {
  it("shows the agent, its work and its container output, re-read while open", async () => {
    const { unmount } = render(<AgentDetail agentId="agent-1" onClose={() => {}} />);
    await flush();
    expect(screen.getByText("nanoclaw/1.2 · codex · unknown")).toBeTruthy();
    expect(screen.getByText("Global - Finance canary")).toBeTruthy();
    expect(screen.getByText("cloned the repo")).toBeTruthy();
    expect(screen.getByText(/session started/)).toBeTruthy();
    expect(mock.fetchText).toHaveBeenCalledWith("/api/instances/inst-1/logs?tail=200");

    await flush(4_000);
    expect(mock.fetchText).toHaveBeenCalledTimes(3); // t=0, 2s, 4s
    unmount();
    await flush(10_000);
    expect(mock.fetchText).toHaveBeenCalledTimes(3); // closing stops the reads
  });

  it("stops reading while paused", async () => {
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
