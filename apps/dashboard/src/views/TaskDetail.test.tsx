// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { download } from "../api";
import { TaskDetail } from "./TaskDetail";

const mock = vi.hoisted(() => ({
  task: {} as Record<string, unknown>,
  trace: {} as Record<string, unknown>,
  artifacts: [] as Record<string, unknown>[],
  events: [] as Record<string, unknown>[],
  deliveryGate: null as Promise<void> | null,
  paths: [] as string[],
  prereq: {} as Record<string, unknown>,
  prereqFail: [] as Error[],
}));
vi.mock("../api", () => ({
  api: { GET: (path: string) => path },
  call: async (path: string) => {
    mock.paths.push(path);
    if (path.includes("prerequisites")) {
      const failure = mock.prereqFail.shift();
      if (failure) throw failure;
      return mock.prereq;
    }
    if (path.includes("delivery") && mock.deliveryGate) await mock.deliveryGate;
    return path.includes("projects/stats")
      ? []
      : path.includes("/models")
        ? mock.trace
        : path.includes("delivery")
          ? null
          : path.includes("artifacts")
            ? mock.artifacts
            : path.includes("events")
              ? mock.events
              : path.includes("/attempts")
                ? []
                : mock.task;
  },
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
vi.mock("./TaskCost", () => ({ TaskCostTile: () => null }));
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
  mock.deliveryGate = null;
  mock.paths = [];
  mock.prereq = {};
  mock.prereqFail = [];
});
describe("TaskDetail loading", () => {
  it("paints the task before the forge delivery read settles", async () => {
    mock.task = base;
    mock.trace = { executions: [], timeline: [] };
    let release!: () => void;
    mock.deliveryGate = new Promise<void>((resolve) => {
      release = resolve;
    });
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    expect(await screen.findByText(/trace task/)).toBeTruthy();
    release();
  });
});

describe("TaskDetail continuation", () => {
  it("shows a collapsed Continuation section and does not fetch it before open", async () => {
    mock.task = base;
    mock.trace = { executions: [], timeline: [] };
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    await screen.findByText(/trace task/);
    const section = screen.getByLabelText("Continuation") as HTMLDetailsElement;
    expect(section.open).toBe(false);
    expect(mock.paths.some((p) => p.includes("continuation"))).toBe(false);
  });
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

  it("offers Raise budget instead of an Unblock the API would refuse", async () => {
    mock.task = { ...base, state: "blocked", block_reason: "budget_exhausted", budget_usd: 2 };
    mock.trace = { executions: [], timeline: [] };
    await act(async () => {
      render(<TaskDetail taskId="t1" onClose={() => {}} />);
    });
    expect(screen.getByText(/its budget is spent/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Raise budget" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Unblock" })).toBeNull();
  });
});

describe("TaskDetail workspace", () => {
  const show = async (workspace: Record<string, unknown> | null) => {
    mock.task = { ...base, workspace };
    mock.trace = { executions: [], timeline: [] };
    await act(async () => {
      render(<TaskDetail taskId="t1" onClose={() => {}} />);
    });
  };

  it.each([
    ["available", /work can be restored/],
    ["unavailable", /work cannot be restored or inspected/],
    ["unknown", /none recorded yet, still running, or state not recognised/],
  ])("shows the owner and a %s checkpoint", async (checkpoint, text) => {
    await show({
      owner_agent_id: "agent-7",
      owner_status: "live",
      owner_node_id: "host-a",
      checkpoint,
    });
    expect(screen.getByText("Workspace")).toBeTruthy();
    expect(screen.getByText("agent-7")).toBeTruthy();
    expect(screen.getByText(text)).toBeTruthy();
    expect(screen.getByText(/owner is live/)).toBeTruthy();
    expect(screen.getByText(/on host-a/)).toBeTruthy();
  });

  it("shows a silent owner and omits a node it never reported", async () => {
    await show({
      owner_agent_id: "agent-7",
      owner_status: "stale",
      owner_node_id: null,
      checkpoint: "available",
    });
    expect(screen.getByText(/owner went silent/)).toBeTruthy();
    expect(screen.queryByText(/on host-a/)).toBeNull();
  });

  it("explains a null workspace instead of hiding the row", async () => {
    await show(null);
    expect(screen.getByText("Workspace")).toBeTruthy();
    expect(screen.getByText(/has not started an attempt/)).toBeTruthy();
  });
});

describe("TaskDetail retry", () => {
  it("titles the Retry button with failures, not claims, against max_attempts", async () => {
    mock.task = { ...base, state: "failed", attempt: 8, failures: 1, max_attempts: 3 };
    mock.trace = { executions: [], timeline: [] };
    await act(async () => {
      render(<TaskDetail taskId="t1" onClose={() => {}} />);
    });
    const title = screen.getByRole("button", { name: "Retry" }).getAttribute("title");
    expect(title).toContain("1/3");
    expect(title).not.toContain("8/3");
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

describe("TaskDetail dependency delivery", () => {
  const dep = (over: Record<string, unknown> = {}, evidence: Record<string, unknown> = {}) => ({
    task_id: "dep1",
    title: "Build the parser",
    state: "succeeded",
    task_succeeded: true,
    verdict: "unsatisfied",
    why: "open",
    evidence: {
      pr: "o/r#7",
      pr_state: "open",
      checked_at: "2026-11-07T10:00:00Z",
      source: "forge",
      artifacts: [],
      notes: [],
      ...evidence,
    },
    ...over,
  });
  const view = (items: unknown[], over: Record<string, unknown> = {}) => ({
    task_id: "t1",
    version: "v",
    enforced: false,
    expected_base: null,
    items,
    omitted: [],
    partial: [],
    ...over,
  });
  const open = async () => {
    mock.task = { ...base, depends_on: ["dep1"] };
    mock.trace = { executions: [], timeline: [] };
    await act(async () => {
      render(<TaskDetail taskId="t1" onClose={() => {}} />);
    });
    const section = screen.getByLabelText("Dependency delivery") as HTMLDetailsElement;
    expect(mock.paths.some((p) => p.includes("prerequisites"))).toBe(false);
    await act(async () => {
      section.open = true;
      section.dispatchEvent(new Event("toggle"));
    });
  };
  const requests = () => mock.paths.filter((p) => p.includes("prerequisites")).length;

  it("is absent without dependencies", async () => {
    mock.task = base;
    mock.trace = { executions: [], timeline: [] };
    await act(async () => {
      render(<TaskDetail taskId="t1" onClose={() => {}} />);
    });
    expect(screen.queryByLabelText("Dependency delivery")).toBeNull();
  });

  it("loads lazily once and shows title, link, state, verdict, why and freshness apart", async () => {
    mock.prereq = view([dep()]);
    await open();
    expect(requests()).toBe(1);
    const link = screen.getByRole("link", { name: "Build the parser" });
    expect(link.getAttribute("href")).toBe("#tasks/dep1");
    expect(screen.getByText("Task state").nextElementSibling?.textContent).toBe(
      "succeeded (succeeded)",
    );
    const verdict = screen.getByText("Delivery verdict").nextElementSibling?.textContent;
    expect(verdict).toContain("unsatisfied");
    expect(verdict).toContain("why: open");
    expect(screen.getByText("Checked").nextElementSibling?.textContent).toContain("source: forge");
    expect(screen.getByText(/grants no claim, merge or acceptance permission/)).toBeTruthy();
  });

  it("keeps unknown as unknown and a failed task apart from delivery", async () => {
    mock.prereq = view([
      dep(
        { state: "failed", task_succeeded: false, verdict: "unknown", why: "forge_unavailable" },
        {
          pr_state: "unknown",
          checked_at: null,
          source: null,
          artifacts: [
            {
              id: "a1",
              name: "out.tgz",
              sha256: "x",
              size: 5,
              verified: "unknown",
              checked_at: "",
            },
          ],
        },
      ),
    ]);
    await open();
    expect(screen.getByText("Task state").nextElementSibling?.textContent).toBe(
      "failed (not succeeded)",
    );
    expect(screen.getByText("Delivery verdict").nextElementSibling?.textContent).toContain(
      "unknown",
    );
    expect(screen.getByText("Checked").nextElementSibling?.textContent).toBe("not checked");
    expect(screen.getByText(/out\.tgz · verified: unknown/)).toBeTruthy();
  });

  it("discloses partial and omitted dependencies", async () => {
    mock.prereq = view([dep({ verdict: "satisfied", why: "merged" })], {
      partial: ["forge:o/r#7"],
      omitted: ["dependencies"],
    });
    await open();
    expect(screen.getByText(/Incomplete reads: forge:o\/r#7/)).toBeTruthy();
    expect(screen.getByText(/Omitted \(truncated\): dependencies/)).toBeTruthy();
  });

  it("keeps a failure local and Retry re-issues one request", async () => {
    mock.prereqFail = [new Error("boom")];
    mock.prereq = view([dep()]);
    await open();
    expect(screen.getByText(/Could not load prerequisites: boom/)).toBeTruthy();
    expect(screen.getByText(/trace task/)).toBeTruthy();
    expect(requests()).toBe(1);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    });
    expect(requests()).toBe(2);
    expect(screen.getByRole("link", { name: "Build the parser" })).toBeTruthy();
  });
});
