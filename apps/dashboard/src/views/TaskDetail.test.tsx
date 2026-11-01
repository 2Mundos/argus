// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { download } from "../api";
import { TaskDetail } from "./TaskDetail";

const mock = vi.hoisted(() => ({
  task: {} as Record<string, unknown>,
  trace: {} as Record<string, unknown>,
  artifacts: [] as Record<string, unknown>[],
  events: [] as Record<string, unknown>[],
}));
vi.mock("../api", () => ({
  api: { GET: (path: string) => path },
  call: async (path: string) =>
    path.includes("/models")
      ? mock.trace
      : path.includes("delivery")
        ? null
        : path.includes("artifacts")
          ? mock.artifacts
          : path.includes("events")
            ? mock.events
            : mock.task,
  download: vi.fn(),
}));
vi.mock("../live", async () => {
  const React = await import("react");
  return {
    useLiveData: (load: () => Promise<unknown>) => {
      const [data, setData] = React.useState<unknown>();
      React.useEffect(() => {
        load().then(setData);
      }, []);
      return { data, reload: () => {} };
    },
  };
});
vi.mock("./Activity", () => ({ Activity: () => null }));
vi.mock("../WorkerChip", () => ({
  WorkerChip: ({ agentId }: { agentId: string }) => (
    <span data-testid="worker-chip">{agentId}</span>
  ),
  WorkerActorChip: ({ actor }: { actor: string }) => <span>{actor}</span>,
}));
vi.mock("./EventList", () => ({
  EventList: ({ events }: { events: { id: string; kind: string }[] }) => (
    <ul aria-label="History events">
      {events.map((e) => (
        <li key={e.id}>{e.kind}</li>
      ))}
    </ul>
  ),
}));
vi.mock("./TaskEditForm", () => ({ TaskEditForm: () => null }));
vi.mock("./TaskTerminal", () => ({ TaskTerminal: () => null }));
vi.mock("../delivery", () => ({ TaskDelivery: () => null }));
vi.mock("../failover", () => ({ TaskFailover: () => null }));

const base = {
  id: "t1",
  title: "trace task",
  project: "global",
  state: "succeeded",
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
};

afterEach(() => {
  cleanup();
  mock.artifacts = [];
  mock.events = [];
});
describe("TaskDetail blocked reason", () => {
  it("shows the inbox card sentence for a block_reason", async () => {
    mock.task = { ...base, state: "blocked", block_reason: "env_repository_unconfigured" };
    mock.trace = { executions: [], timeline: [] };
    await act(async () => {
      render(<TaskDetail taskId="t1" onClose={() => {}} />);
    });
    expect(screen.getByText(/its project has no repository configured/)).toBeTruthy();
  });

  it("shows no block reason without a block_reason", async () => {
    mock.task = { ...base, state: "ready", block_reason: null };
    mock.trace = { executions: [], timeline: [] };
    await act(async () => {
      render(<TaskDetail taskId="t1" onClose={() => {}} />);
    });
    expect(screen.queryByText(/repository configured/)).toBeNull();
    expect(screen.queryByText(/OOM-killed/)).toBeNull();
  });

  it("shows the env_oom sentence and the OOM-killed note on the state line", async () => {
    mock.task = { ...base, state: "blocked", block_reason: "env_oom", oom_kill: true };
    mock.trace = { executions: [], timeline: [] };
    await act(async () => {
      render(<TaskDetail taskId="t1" onClose={() => {}} />);
    });
    const state = screen.getByText("blocked").closest("dd");
    expect(state?.textContent).toMatch(/keep running out of memory/);
    expect(state?.textContent).toMatch(/OOM-killed/);
  });
});

describe("TaskDetail model trace", () => {
  it("renders attempt grouped harness and delegate executions, evidence, accounts, usage and switches", async () => {
    mock.task = base;
    mock.trace = {
      executions: [
        {
          id: "h",
          attempt: 2,
          layer: "harness",
          engine: "codex",
          state: "succeeded",
          observed_model: "gpt-6",
          evidence: "cli_output",
          account_name: "Harness acct",
          usage: { input_tokens: 8, output_tokens: 3, cost_usd: 0.12 },
        },
        {
          id: "d",
          attempt: 2,
          layer: "delegate",
          executor: "claude",
          state: "failed",
          configured_model: "opus",
          evidence: "not_reported",
          account_name: "Delegate acct",
        },
        {
          id: "unknown",
          attempt: 1,
          layer: "delegate",
          executor: "codex",
          state: "failed",
          evidence: "not_reported",
        },
      ],
      timeline: [
        {
          id: "s",
          attempt: 2,
          kind: "model_switch",
          from_model: "sonnet",
          to_model: "opus",
          reason_code: "verification_failed",
        },
      ],
    };
    await act(async () => {
      render(<TaskDetail taskId="t1" onClose={() => {}} />);
    });
    expect(screen.getByRole("region", { name: "Attempt 2 models" })).toBeTruthy();
    const attemptTwo = screen.getByRole("region", { name: "Attempt 2 models" });
    expect(attemptTwo.querySelectorAll("p")[0]?.textContent).toContain(
      "codex gpt-6 (cli_output) · account Harness acct",
    );
    expect(attemptTwo.querySelectorAll("p")[1]?.textContent).toContain(
      "claude opus (configured) · account Delegate acct",
    );
    expect(screen.getByText(/8\+3 tokens · \$0.12/)).toBeTruthy();
    expect(screen.getByText(/model_switch · verification_failed · sonnet → opus/)).toBeTruthy();
    expect(screen.getByText(/not reported/)).toBeTruthy();
  });
});

describe("TaskDetail artifacts", () => {
  it("shows name, size, media type and source, and downloads from the content path", async () => {
    mock.task = base;
    mock.trace = { executions: [], timeline: [] };
    mock.artifacts = [
      {
        id: "a1",
        attempt: 0,
        name: "shot.png",
        size: 2048,
        media_type: "image/png",
        source: "attachment",
      },
    ];
    await act(async () => {
      render(<TaskDetail taskId="t1" onClose={() => {}} />);
    });
    const item = screen.getByText("shot.png").closest("li");
    expect(item?.textContent).toContain("2.0 KB");
    expect(item?.textContent).toContain("image/png");
    expect(item?.textContent).toContain("attachment");
    await act(async () => {
      screen.getByText("shot.png").click();
    });
    expect(download).toHaveBeenCalledWith("/api/artifacts/a1/content", "shot.png");
  });
});

describe("TaskDetail history window", () => {
  const event = (i: number, kind: string) => ({ id: `e${i}`, kind, attempt: 1 });
  const truncation = /Only the newest 50 events are shown\. Older events are not listed\./;
  async function open() {
    mock.task = base;
    mock.trace = { executions: [], timeline: [] };
    await act(async () => {
      render(<TaskDetail taskId="t1" onClose={() => {}} />);
    });
  }

  it("lists a prior attempt's events that are inside the page", async () => {
    mock.events = [event(2, "task.retried"), event(1, "task.failed")];
    await open();
    const list = screen.getByRole("list", { name: "History events" });
    expect(list.textContent).toContain("task.retried");
    expect(list.textContent).toContain("task.failed");
  });

  it("says older events are not listed when the page is full", async () => {
    mock.events = Array.from({ length: 50 }, (_, i) => event(i, "task.progress"));
    await open();
    expect(screen.getByText(truncation)).toBeTruthy();
  });

  it("shows no truncation line for a shorter page", async () => {
    mock.events = Array.from({ length: 49 }, (_, i) => event(i, "task.progress"));
    await open();
    expect(screen.queryByText(truncation)).toBeNull();
  });
});
