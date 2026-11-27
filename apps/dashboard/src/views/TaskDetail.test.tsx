// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { download } from "../api";
import { TaskDetail } from "./TaskDetail";
import { Tasks } from "./Tasks";

const mock = vi.hoisted(() => ({
  task: {} as Record<string, unknown>,
  tasksById: {} as Record<string, Record<string, unknown>>,
  boardItems: [] as Record<string, unknown>[],
  trace: {} as Record<string, unknown>,
  artifacts: [] as Record<string, unknown>[],
  events: [] as Record<string, unknown>[],
  deliveryGate: null as Promise<void> | null,
  paths: [] as string[],
  prereq: {} as Record<string, unknown>,
  prereqFail: [] as Error[],
  reviews: { rounds: [] } as Record<string, unknown>,
  posts: [] as { path: string; body: unknown }[],
}));
vi.mock("../api", () => ({
  api: {
    GET: (path: string, opts?: { params?: { path?: { task_id?: string } } }) =>
      opts?.params?.path?.task_id ? path.replace("{task_id}", opts.params.path.task_id) : path,
    POST: (path: string, opts?: { body?: unknown }) => {
      mock.posts.push({ path, body: opts?.body });
      return path;
    },
  },
  ENDED_STATES: ["succeeded", "failed", "cancelled"],
  boardTasks: async () => ({
    items: mock.boardItems,
    total: mock.boardItems.length,
    loading: false,
  }),
  searchErrorMessage: (err: Error) => ({ text: err.message }),
  call: async (path: string) => {
    mock.paths.push(path);
    if (path.includes("prerequisites")) {
      const failure = mock.prereqFail.shift();
      if (failure) throw failure;
      return mock.prereq;
    }
    if (path.endsWith("/reviews")) return mock.reviews;
    if (path.includes("delivery") && mock.deliveryGate) await mock.deliveryGate;
    if (path.includes("projects/stats")) return [];
    if (path.includes("/models")) return mock.trace;
    if (path.includes("delivery") || path.endsWith("/route")) return null;
    if (path.includes("artifacts")) return mock.artifacts;
    if (path.includes("events")) return mock.events;
    if (path.includes("/attempts")) return [];
    const taskId = path.match(/^\/api\/tasks\/([^/]+)$/)?.[1];
    if (taskId && taskId in mock.tasksById) return mock.tasksById[taskId];
    return mock.task;
  },
  download: vi.fn(),
}));
vi.mock("../live", async () => {
  const React = await import("react");
  return {
    reportPage: () => {},
    useLiveData: (load: () => Promise<unknown>, deps: readonly unknown[] = []) => {
      const [data, setData] = React.useState<unknown>();
      React.useEffect(() => {
        let cancelled = false;
        load().then((value) => {
          if (!cancelled) setData(value);
        });
        return () => {
          cancelled = true;
        };
      }, deps);
      return { data, reload: () => {} };
    },
  };
});
vi.mock("./Activity", () => ({ Activity: () => null, useNow: () => 0 }));
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
  merge: {
    authorized: false,
    may_merge: false,
    deliverable: "response",
    auto_merge: false,
    auto_merge_source: "default",
    allow_merge: false,
    granted_by: null,
  },
  forge_review: "off",
  pr_gate: null,
};

afterEach(() => {
  cleanup();
  mock.tasksById = {};
  mock.boardItems = [];
  mock.artifacts = [];
  mock.events = [];
  mock.deliveryGate = null;
  mock.paths = [];
  mock.prereq = {};
  mock.prereqFail = [];
  mock.reviews = { rounds: [] };
  window.location.hash = "";
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

describe("TaskDetail last error", () => {
  const shown = async (patch: object) => {
    mock.task = { ...base, ...patch };
    mock.trace = { executions: [], timeline: [] };
    await act(async () => {
      render(<TaskDetail taskId="t1" onClose={() => {}} />);
    });
  };

  it("labels an error kept across a requeue and a new claim as the previous attempt's", async () => {
    for (const state of ["ready", "claimed"]) {
      cleanup();
      await shown({
        state,
        attempt: 2,
        error: "auth_missing for anthropic",
        error_attempt: 1,
        error_scope: "previous",
      });
      expect(screen.getByText(/Previous attempt's error \(attempt 1\): auth_missing/)).toBeTruthy();
      expect(screen.queryByText(/Last error/)).toBeNull();
    }
  });

  it("keeps the plain label for a current failure and shows nothing without an error", async () => {
    await shown({
      state: "failed",
      error: "boom",
      error_attempt: 2,
      error_scope: "current",
    });
    expect(screen.getByText(/Last error \(attempt 2\): boom/)).toBeTruthy();
    cleanup();
    await shown({ state: "claimed", error: null, error_attempt: null, error_scope: null });
    expect(screen.queryByText(/error/i)).toBeNull();
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

  describe("artifact evidence", () => {
    const art = (id: string, over: Record<string, unknown> = {}) => ({
      id,
      name: `${id}.tgz`,
      sha256: "x",
      size: 5,
      verified: true,
      checked_at: "2026-11-07T10:00:00Z",
      ...over,
    });
    const artifactDep = (artifacts: unknown[]) =>
      dep({}, { pr: null, pr_state: null, checked_at: null, source: null, artifacts });
    const item = (name: string) => screen.getByText(new RegExp(`^${name}\\.tgz`)).closest("li");

    it("shows each artifact's own checked time with per-artifact attribution", async () => {
      mock.prereq = view([
        artifactDep([
          art("a1", { checked_at: "2026-11-07T10:00:00Z" }),
          art("a2", { checked_at: "2026-11-08T12:30:00Z" }),
        ]),
      ]);
      await open();
      const first = item("a1");
      const second = item("a2");
      expect(first?.textContent).toContain("a1.tgz · verified: true · 5 bytes · checked ");
      expect(first?.querySelector("time")?.getAttribute("title")).toBe("2026-11-07T10:00:00.000Z");
      expect(second?.querySelector("time")?.getAttribute("title")).toBe("2026-11-08T12:30:00.000Z");
      // The row-level line stays a PR-level fact and is not filled from artifacts.
      const checked = screen.getByText("Checked").nextElementSibling;
      expect(checked?.querySelector("time")).toBeNull();
      expect(checked?.textContent).toBe("PR not checked");
    });

    it("keeps a missing artifact checked_at explicitly unknown", async () => {
      mock.prereq = view([
        artifactDep([art("a1"), art("a2", { checked_at: null }), art("a3", { checked_at: "" })]),
      ]);
      await open();
      expect(item("a1")?.querySelector("time")).not.toBeNull();
      for (const name of ["a2", "a3"]) {
        expect(item(name)?.querySelector("time")).toBeNull();
        expect(item(name)?.textContent).toContain("checked: unknown");
      }
    });

    it("keeps true, false and unknown verified statuses distinct", async () => {
      mock.prereq = view([
        artifactDep([
          art("a1", { verified: true }),
          art("a2", { verified: false }),
          art("a3", { verified: "unknown" }),
        ]),
      ]);
      await open();
      expect(item("a1")?.textContent).toContain("verified: true");
      expect(item("a2")?.textContent).toContain("verified: false");
      expect(item("a3")?.textContent).toContain("verified: unknown");
    });

    it("keeps the read-only evidence note when not enforced", async () => {
      mock.prereq = view([artifactDep([art("a1")])], { enforced: false });
      await open();
      expect(screen.getByText(/enforced: false/)).toBeTruthy();
      expect(screen.getByText(/grants no claim, merge or acceptance permission/)).toBeTruthy();
    });
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

describe("TaskDetail merge and review decisions", () => {
  const text = async () => {
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    await screen.findByText(/trace task/);
    return screen.getByLabelText("Merge and review").textContent;
  };

  it("says a pr_green task may not merge and review is off", async () => {
    mock.task = { ...base, merge: { ...base.merge, deliverable: "pr_green" } };
    mock.trace = { executions: [], timeline: [] };
    const line = await text();
    expect(line).toContain("Merge: not authorized — deliverable pr_green; enable auto-merge");
    expect(line).toContain("Review: off");
  });

  it("names global auto-merge for a pr_merged task that may merge", async () => {
    mock.task = {
      ...base,
      merge: {
        ...base.merge,
        authorized: true,
        may_merge: true,
        deliverable: "pr_merged",
        auto_merge: true,
        auto_merge_source: "global",
        granted_by: "auto_merge",
      },
    };
    mock.trace = { executions: [], timeline: [] };
    expect(await text()).toContain(
      "Merge: authorized by global auto-merge (deliverable pr_merged)",
    );
  });

  it("shows required review and the PR gate reasons for a bound PR without approvals", async () => {
    mock.task = {
      ...base,
      forge_review: "required",
      pr_gate: {
        pr: "https://git.example.test/org/repo/pulls/7",
        pr_state: "open",
        head_sha: "a".repeat(40),
        ready: false,
        reasons: ["review_required"],
        review_policy: "required",
        approvals_for_head: 0,
      },
    };
    mock.trace = { executions: [], timeline: [] };
    const line = await text();
    expect(line).toContain("Review: required by forge review policy");
    expect(line).toContain("PR gate at aaaaaaa: review required, 0 approvals");
  });
});

describe("TaskDetail review rounds", () => {
  const round = (n: number, verdict: string, head: string, findings: unknown[]) => ({
    child_id: `child-${n}`,
    round: n,
    state: "succeeded",
    verdict,
    head_sha: head,
    findings,
    findings_by_priority: {},
    reviewer_provider: "openai",
    reviewer_model: `gpt-${n}`,
    artifact_ids: [`art-${n}`],
    created_at: "2026-10-20T12:00:00Z",
    finished_at: "2026-10-20T12:05:00Z",
  });

  it("shows one tab per round, newest selected, and switches on click", async () => {
    mock.task = base;
    mock.reviews = {
      rounds: [
        round(1, "request_changes", "a".repeat(40), [
          { priority: "P1", file: "a.py", lines: "7", description: "leak" },
        ]),
        round(2, "approve", "b".repeat(40), []),
      ],
    };
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    const tabs = await screen.findAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual([
      "Round 1 · changes requested",
      "Round 2 · approved",
    ]);
    expect(tabs[1]?.getAttribute("aria-selected")).toBe("true");
    expect(screen.getByText("bbbbbbbbbbbb")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Review task/ }).getAttribute("href")).toBe(
      "#tasks/child-2",
    );
    fireEvent.click(screen.getByRole("button", { name: /report/ }));
    expect(download).toHaveBeenCalledWith("/api/artifacts/art-2/content", "art-2");
    fireEvent.click(tabs[0] as HTMLElement);
    expect(screen.getByText("leak")).toBeTruthy();
    expect(screen.getByText("a.py:7")).toBeTruthy();
    expect(screen.getByRole("link", { name: /Review task/ }).getAttribute("href")).toBe(
      "#tasks/child-1",
    );
  });

  it("links a review child back to its parent and shows no tabs on it", async () => {
    mock.task = { ...base, kind: "code_review", parent_id: "parent-1" };
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    const link = await screen.findByRole("link", { name: "parent task" });
    expect(link.getAttribute("href")).toBe("#tasks/parent-1");
    expect(screen.queryByRole("tablist")).toBeNull();
  });

  it("navigates between parent and review round child through hash links in Tasks", async () => {
    const parent = {
      ...base,
      id: "parent-1",
      title: "Parent task title",
      state: "in_review",
      priority: 0,
    };
    const child = {
      ...base,
      id: "child-1",
      title: "Review round child title",
      kind: "code_review",
      parent_id: "parent-1",
      priority: 0,
    };
    mock.trace = { executions: [], timeline: [] };
    mock.boardItems = [parent];
    mock.tasksById = { "parent-1": parent, "child-1": child };
    mock.reviews = {
      rounds: [round(1, "approve", "a".repeat(40), [])],
    };
    render(<Tasks />);
    fireEvent.click(await screen.findByRole("button", { name: /Parent task title/ }));
    const reviewLink = await screen.findByRole("link", { name: /Review task/ });
    expect(reviewLink.getAttribute("href")).toBe("#tasks/child-1");

    fireEvent.click(reviewLink);
    expect(await screen.findByRole("heading", { name: /Review round child title/ })).toBeTruthy();
    const parentLink = screen.getByRole("link", { name: "parent task" });
    expect(parentLink.getAttribute("href")).toBe("#tasks/parent-1");

    fireEvent.click(parentLink);
    expect(await screen.findByRole("heading", { name: /Parent task title/ })).toBeTruthy();
    expect(await screen.findByRole("link", { name: /Review task/ })).toBeTruthy();
  });
});

describe("TaskDetail Accept and close", () => {
  const open = async (state: string) => {
    mock.task = { ...base, state, version: "v1" };
    mock.trace = { executions: [], timeline: [] };
    mock.posts = [];
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    await screen.findByText(/trace task/);
  };

  it.each(["pending", "ready", "claimed", "blocked", "in_review", "awaiting_quota"])(
    "sits beside Cancel task for a %s task",
    async (state) => {
      await open(state);
      expect(screen.getByRole("button", { name: "Accept and close" })).toBeTruthy();
      expect(screen.getByRole("button", { name: "Cancel task" })).toBeTruthy();
    },
  );

  it.each(["succeeded", "failed", "cancelled"])("is absent for a %s task", async (state) => {
    await open(state);
    expect(screen.queryByRole("button", { name: "Accept and close" })).toBeNull();
  });

  it("requires a reason before it calls the API, then sends it with the version", async () => {
    await open("in_review");
    fireEvent.click(screen.getByRole("button", { name: "Accept and close" }));
    const confirm = screen.getByRole("button", { name: "Confirm accept and close" });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Reason (required)"), { target: { value: "   " } });
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    expect(mock.posts).toEqual([]);
    fireEvent.change(screen.getByLabelText("Reason (required)"), {
      target: { value: " owner merged PR " },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Confirm accept and close" }));
    });
    expect(mock.posts).toEqual([
      {
        path: "/api/tasks/{task_id}/force-accept",
        body: { reason: "owner merged PR", expected_version: "v1" },
      },
    ]);
  });

  it("renders the force_accepted disposition with its reason", async () => {
    mock.task = {
      ...base,
      state: "succeeded",
      review_disposition: {
        outcome: "force_accepted",
        reason: "owner merged PR",
        unmet: [],
        decided_by: "admin",
        decided_at: "2026-11-27T00:00:00Z",
        attempt: 1,
      },
    };
    mock.trace = { executions: [], timeline: [] };
    render(<TaskDetail taskId="t1" onClose={() => {}} />);
    await screen.findByText(/trace task/);
    expect(screen.getByText(/accepted and closed by the owner/)).toBeTruthy();
    expect(screen.getByText(/owner merged PR/)).toBeTruthy();
  });
});
