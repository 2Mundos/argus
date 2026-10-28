// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Inbox } from "./Inbox";

const net = vi.hoisted(() => {
  const net = {
    inbox: {} as unknown,
    inboxQueries: [] as string[],
    posts: [] as { answers: Record<string, unknown>[] }[],
    replies: [] as unknown[],
  };
  const Base = globalThis.Request;
  globalThis.Request = class extends Base {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      super(
        typeof input === "string" && input.startsWith("/") ? `http://localhost${input}` : input,
        init,
      );
    }
  } as typeof Request;
  globalThis.fetch = (async (input: Request) => {
    const url = new URL(input.url);
    let body: unknown = [];
    if (url.pathname === "/api/inbox") {
      net.inboxQueries.push(url.search);
      body = net.inbox;
    } else if (url.pathname === "/api/decisions/answers") {
      net.posts.push(await input.json());
      body = net.replies.shift();
    } else if (url.pathname.startsWith("/api/tasks/")) {
      body = { title: "Deploy", project: "alpha" };
    }
    return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
  return net;
});

vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  return {
    useLiveData: <T,>(load: () => Promise<T>, deps: unknown[]) => {
      const [data, setData] = useState<T>();
      // biome-ignore lint/correctness/useExhaustiveDependencies: deps are the caller's
      useEffect(() => {
        load().then(setData);
      }, deps);
      return { data, error: undefined, reload: () => {} };
    },
  };
});

afterEach(() => {
  cleanup();
  net.posts.length = 0;
  net.inboxQueries.length = 0;
});

function sent(i: number) {
  const post = net.posts[i];
  if (!post) throw new Error(`no POST #${i}`);
  return post.answers;
}

function decision(id: string, kind: string, question: string, options: string[] | null) {
  return {
    id,
    kind,
    question,
    options,
    audience: "any",
    subject_type: "task",
    subject_id: `task-${id}`,
    subject_title: "Deploy",
    requested_by: "agent:abcdef123456",
    created_at: new Date().toISOString(),
    truncated: {},
  };
}

function item(id: string, kind: string, question: string, options: string[] | null, impact = 0) {
  return {
    subject_type: "task",
    subject_id: `task-${id}`,
    title: `Task ${id}`,
    project: "alpha",
    state: "blocked",
    decision_id: id,
    decision_kind: kind,
    audience: "any",
    age_s: 120,
    group: `task:task-${id}`,
    blocks: Array.from({ length: impact }, (_, i) => ({
      id: `dep-${i}`,
      title: `Dependent ${i}`,
      state: "pending",
    })),
    impact,
    artifact_ids: [],
    failed_dependencies: [],
    decision: decision(id, kind, question, options),
  };
}

function group(id: string, stalled: number) {
  return {
    key: `task:task-${id}`,
    project: "alpha",
    subject_type: "task",
    subject_id: `task-${id}`,
    title: `Task ${id}`,
    open_decisions: 1,
    oldest_age_s: 7200,
    stalled_tasks: stalled,
  };
}

function seed() {
  net.inbox = {
    items: [
      item("d1", "approval", "Ship it?", ["approve", "reject"], 2),
      item("d2", "question", "Which region?", null),
    ],
    groups: [group("d1", 2), group("d2", 0)],
    total: 2,
    has_more: false,
  };
}

describe("Inbox groups", () => {
  it("shows project, task group, count, oldest age, type and impact; passes filters", async () => {
    seed();
    render(<Inbox />);
    expect(await screen.findByRole("heading", { name: "alpha" })).toBeTruthy();
    expect(screen.getByText(/1 open · oldest 2h · 2 stalled tasks/)).toBeTruthy();
    expect(screen.getByText(/Blocks 2 tasks: Dependent 0, Dependent 1/)).toBeTruthy();
    expect(screen.getByText("Blocks no tasks")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Blocking only"));
    fireEvent.change(screen.getByLabelText("Sort"), { target: { value: "impact" } });
    await waitFor(() => expect(net.inboxQueries.at(-1)).toContain("blocking_only=true"));
    expect(net.inboxQueries.at(-1)).toContain("order=impact");
  });
});

describe("Inbox blocked tasks", () => {
  it("says why a worker blocked a task and shows its error", async () => {
    const { decision: _, ...base } = item("b1", "question", "", null);
    net.inbox = {
      items: [
        {
          ...base,
          decision_id: null,
          decision_kind: null,
          task: { block_reason: "env_executor_auth", error: "codex: not logged in" },
        },
      ],
      groups: [group("b1", 1)],
      total: 1,
      has_more: false,
    };
    render(<Inbox />);
    expect(await screen.findByText(/is\s+blocked: the executor could not log in/)).toBeTruthy();
    expect(screen.getByText("codex: not logged in")).toBeTruthy();
  });
});

describe("Inbox quiet claims", () => {
  function quiet(id: string, stall_cause: string | null) {
    const { decision: _, ...base } = item(id, "question", "", null);
    return {
      ...base,
      title: `Task ${id}`,
      state: "claimed",
      decision_id: null,
      decision_kind: null,
      age_s: 1500,
      last_signal_at: null,
      last_signal_source: null,
      stall_cause,
    };
  }

  it("names the likely stall cause, and nothing when it is unknown", async () => {
    net.inbox = {
      items: [quiet("q1", "workspace_missing"), quiet("q2", "unknown")],
      groups: [group("q1", 0), group("q2", 0)],
      total: 2,
      has_more: false,
    };
    render(<Inbox />);
    expect(
      await screen.findByText(/is quiet\s+for 25m; likely cause: workspace missing on the host\./),
    ).toBeTruthy();
    const rows = [...document.querySelectorAll("article.quiet-claim > p:first-child")];
    expect(rows.map((p) => p.textContent?.replace(/\s+/g, " "))).toEqual([
      "Alpha - Task q1 is quiet for 25m; likely cause: workspace missing on the host.",
      "Alpha - Task q2 is quiet for 25m.",
    ]);
  });
});

describe("Inbox batch answers", () => {
  it("confirms each answer, shows per-item results and retries with the same request_id", async () => {
    seed();
    render(<Inbox />);
    await screen.findByText("Ship it?");
    const [first, second] = screen.getAllByLabelText("Add to batch") as [HTMLElement, HTMLElement];
    fireEvent.click(first);
    fireEvent.click(second);
    // Nothing is answerable yet: each decision needs its own answer.
    const review = screen.getByRole("button", { name: /Review 2 answers/ }) as HTMLButtonElement;
    expect(review.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText(/Batch answer for Ship it\?/), {
      target: { value: "approve" },
    });
    fireEvent.change(screen.getAllByRole("textbox")[1] as HTMLElement, {
      target: { value: "eu-west" },
    });
    expect(review.disabled).toBe(false);

    fireEvent.click(review);
    const panel = screen.getByRole("region", { name: "Confirm batch answers" });
    expect(within(panel).getByText("Ship it?")).toBeTruthy();
    expect(within(panel).getByText("approve")).toBeTruthy();
    expect(within(panel).getByText("eu-west")).toBeTruthy();
    expect(net.posts).toHaveLength(0); // never auto-answers

    net.replies = [
      {
        results: [
          { decision_id: "d1", ok: true, replayed: false },
          { decision_id: "d2", ok: false, error: { message: "decision is not open" } },
        ],
      },
      { results: [{ decision_id: "d2", ok: true, replayed: false }] },
    ];
    fireEvent.click(within(panel).getByRole("button", { name: /Send 2 answers/ }));
    expect(await within(panel).findByText("Error: decision is not open")).toBeTruthy();
    expect(within(panel).getByText("ok")).toBeTruthy();
    expect(sent(0)).toEqual([
      { decision_id: "d1", choice: "approve", text: "", request_id: expect.any(String) },
      { decision_id: "d2", choice: null, text: "eu-west", request_id: expect.any(String) },
    ]);
    expect(sent(0)[0]?.request_id).not.toBe(sent(0)[1]?.request_id);

    fireEvent.click(within(panel).getByRole("button", { name: "Retry 1 failed" }));
    await waitFor(() => expect(net.posts).toHaveLength(2));
    expect(sent(1)).toEqual([
      {
        decision_id: "d2",
        choice: null,
        text: "eu-west",
        request_id: sent(0)[1]?.request_id,
      },
    ]);
  });

  it("caps the batch at 20 selected decisions", async () => {
    const ids = Array.from({ length: 21 }, (_, i) => `x${i}`);
    net.inbox = {
      items: ids.map((id) => item(id, "approval", `Q ${id}`, ["approve"])),
      groups: ids.map((id) => group(id, 0)),
      total: 21,
      has_more: false,
    };
    render(<Inbox />);
    await screen.findByText("Q x0");
    for (const box of screen.getAllByLabelText("Add to batch").slice(0, 20)) fireEvent.click(box);
    const boxes = screen.getAllByLabelText("Add to batch") as HTMLInputElement[];
    expect(boxes[20]?.disabled).toBe(true);
  });
});
