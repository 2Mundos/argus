// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskContinuation } from "./TaskContinuation";

const mock = vi.hoisted(() => ({
  paths: [] as string[],
  reload: vi.fn(),
  live: {} as { data?: unknown; error?: Error },
}));
vi.mock("../api", () => ({
  api: { GET: (path: string) => path },
  call: async (path: string) => path,
}));
vi.mock("../live", () => ({
  useLiveData: (load: (s: AbortSignal) => Promise<string>) => {
    void load(new AbortController().signal).then((p) => mock.paths.push(p));
    return { ...mock.live, reload: mock.reload };
  },
}));
vi.mock("../DateChip", () => ({ DateChip: ({ at }: { at: string }) => <span>{at}</span> }));

const base = {
  phase: "resume",
  phase_source: "derived",
  objective: null,
  artifacts: [],
  checklist: [],
  checklist_state: "structured",
  decisions: [],
  environment: [],
  gaps: [],
  pending: [],
  interruption: null,
  reconciliation: [],
  wip: null,
  revision: 3,
};

function open() {
  const view = render(<TaskContinuation taskId="t1" />);
  const details = view.container.querySelector("details") as HTMLDetailsElement;
  act(() => {
    details.open = true;
    fireEvent(details, new Event("toggle"));
  });
  return view;
}

beforeEach(() => {
  mock.live = { data: base };
});
afterEach(() => {
  cleanup();
  mock.paths = [];
  mock.reload.mockReset();
});

describe("TaskContinuation", () => {
  it("is collapsed and requests nothing until opened", async () => {
    const { container } = render(<TaskContinuation taskId="t1" />);
    expect((container.querySelector("details") as HTMLDetailsElement).open).toBe(false);
    expect(mock.paths).toEqual([]);
    expect(screen.queryByText(/Phase:/)).toBeNull();
    open();
    await vi.waitFor(() => expect(mock.paths).toEqual(["/api/tasks/{task_id}/continuation"]));
  });

  it("shows loading, then the error with Retry that reloads", () => {
    mock.live = {};
    open();
    expect(screen.getByText("Loading…")).toBeTruthy();
    cleanup();
    mock.live = { error: new Error("boom") };
    open();
    expect(screen.getByText(/boom/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(mock.reload).toHaveBeenCalledOnce();
  });

  it("shows the phase and whether it is explicit or derived", () => {
    mock.live = { data: { ...base, phase: "qa", phase_source: "explicit" } };
    open();
    expect(screen.getByText("QA")).toBeTruthy();
    expect(screen.getByText("(set explicitly)")).toBeTruthy();
    cleanup();
    mock.live = { data: base };
    open();
    expect(screen.getByText("(derived)")).toBeTruthy();
  });

  it("labels every checklist status distinctly with evidence, never as accepted", () => {
    mock.live = {
      data: {
        ...base,
        checklist: [
          { id: "a", text: "item A", status: "done", evidence: "tests green" },
          { id: "b", text: "item B", status: "pending" },
          { id: "c", text: "item C", status: "verification_missing", evidence: null },
        ],
        pending: ["b", "c"],
      },
    };
    open();
    expect(screen.getAllByText("done")).toHaveLength(1);
    expect(screen.getAllByText("pending")).toHaveLength(1);
    expect(screen.getAllByText("verification missing")).toHaveLength(1);
    expect(screen.getByText("tests green")).toBeTruthy();
    expect(screen.getByText("Pending (2)")).toBeTruthy();
    expect(screen.queryByText(/accepted|passed/i)).toBeNull();
    const rows = screen.getAllByRole("listitem");
    const rowOf = (t: string) =>
      rows.find((r) => r.textContent?.startsWith(t) && r.querySelector(".badge"));
    expect(rowOf("pending")?.textContent).not.toMatch(/\bdone\b/);
    expect(rowOf("verification missing")?.textContent).not.toMatch(/\bdone\b/);
  });

  it.each([
    ["empty", { checklist: [], checklist_state: "structured" }],
    ["unstructured", { checklist: [], checklist_state: "unstructured" }],
    [
      "unstructured with items",
      {
        checklist: [{ id: "a", text: "x", status: "done" }],
        checklist_state: "unstructured",
      },
    ],
  ])("says nothing is confirmed complete for an %s checklist", (_n, patch) => {
    mock.live = { data: { ...base, ...patch } };
    open();
    expect(
      screen.getByText(/No structured checklist exists; nothing is confirmed complete/),
    ).toBeTruthy();
    expect(screen.queryByText("done")).toBeNull();
  });

  it("lists explicit gaps with readable labels", () => {
    mock.live = {
      data: {
        ...base,
        gaps: [
          "artifacts_unknown",
          "acceptance_not_structured",
          "environment_not_ready:gitea",
          "decision_unresolved:d1",
        ],
      },
    };
    open();
    expect(screen.getByText("Gaps (not known)")).toBeTruthy();
    expect(screen.getByText(/Environment not ready: gitea/)).toBeTruthy();
    expect(screen.getByText(/Decision unresolved: d1/)).toBeTruthy();
    expect(screen.getByText("environment_not_ready:gitea")).toBeTruthy();
    expect(screen.getByText("artifacts_unknown")).toBeTruthy();
    expect(screen.getByText("acceptance_not_structured")).toBeTruthy();
  });

  it("shows artifacts, environment, interruption, wip and decisions", () => {
    mock.live = {
      data: {
        ...base,
        artifacts: [
          {
            kind: "pr",
            ref: "#12",
            state: "merged",
            provenance: "runtime_observed",
            observed_at: "2026-11-01T00:00:00Z",
          },
        ],
        environment: [{ resource: "gitea", installed: "yes", reachable: "no" }],
        interruption: { attempt: 1, reason: "expired", at: "2026-11-02T00:00:00Z" },
        wip: { summary: "half done", provenance: "worker_reported", observed_at: "x" },
        decisions: [
          {
            decision_id: "d",
            question: "Which?",
            answer: "A",
            status: "effective",
            premise_outdated: true,
          },
        ],
      },
    };
    open();
    expect(screen.getByText("merged")).toBeTruthy();
    expect(screen.getByText(/runtime observed/)).toBeTruthy();
    expect(
      screen.getByText(/installed: yes, credential usable: unknown, reachable: no/),
    ).toBeTruthy();
    expect(screen.getByText("half done")).toBeTruthy();
    expect(screen.getByText("expired")).toBeTruthy();
    expect(screen.getByText(/premise outdated/)).toBeTruthy();
  });

  it("renders untrusted text as plain text", () => {
    mock.live = {
      data: {
        ...base,
        objective: "<img src=x onerror=alert(1)>",
        wip: { summary: "**x**", provenance: "worker_reported", observed_at: "x" },
      },
    };
    const { container } = open();
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeTruthy();
    expect(screen.getByText("**x**")).toBeTruthy();
    expect(container.querySelector("img, strong + strong")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("bounds long text and long lists", () => {
    const long = "y".repeat(2000);
    mock.live = {
      data: {
        ...base,
        objective: long,
        gaps: Array.from({ length: 14 }, (_, i) => `environment_not_ready:r${i}`),
      },
    };
    const { container } = open();
    expect(container.textContent).not.toContain(long);
    expect(screen.getByText("+4 more")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Read more" }));
    expect(container.textContent).toContain(long);
  });

  it("has no controls besides Retry and Read more", () => {
    mock.live = { data: { ...base, objective: "z".repeat(700) } };
    const { container } = open();
    expect(Array.from(container.querySelectorAll("button")).map((b) => b.textContent)).toEqual([
      "Read more",
    ]);
    expect(container.querySelector("input, textarea, select, form")).toBeNull();
  });
});
