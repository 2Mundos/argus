// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Inbox } from "./Inbox";
import { TaskDetail } from "./TaskDetail";

/** A task blocked on a missing repository, shown in the real drawer and the real inbox. */
const net = vi.hoisted(() => {
  const state = {
    admin: true,
    stats: [] as Record<string, unknown>[],
    project: {} as Record<string, unknown>,
    task: {} as Record<string, unknown>,
    patches: 0,
    unblocks: 0,
    unblockStatus: 200,
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
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  globalThis.fetch = (async (input: Request) => {
    const path = new URL(input.url).pathname;
    if (path === "/api/projects/stats") return json(state.stats);
    if (path === "/api/projects") return json([state.project]);
    if (path.endsWith("/repository") && input.method === "PATCH") {
      if (!state.admin) {
        return json({ detail: { code: "repository_admin_only", message: "admin only" } }, 403);
      }
      state.patches += 1;
      const body = (await input.json()) as { repository_url: string };
      state.project.repository_url = body.repository_url;
      state.stats = [
        { slug: "bare", repository_url: body.repository_url, repository_source: "project" },
      ];
      return json(state.project);
    }
    if (path.endsWith("/unblock")) {
      state.unblocks += 1;
      if (state.unblockStatus !== 200) {
        return json(
          { detail: { code: "invalid_transition", message: "task is ready, not blocked" } },
          state.unblockStatus,
        );
      }
      state.task = { ...state.task, state: "ready", block_reason: null };
      return json(state.task);
    }
    if (path === "/api/tasks/t1") return json(state.task);
    if (path.endsWith("/models")) return json({ executions: [], timeline: [] });
    if (path === "/api/inbox") {
      return json({
        items: [
          {
            subject_type: "task",
            subject_id: "t1",
            title: "Fix login",
            project: "bare",
            state: "blocked",
            decision_id: null,
            decision_kind: null,
            audience: null,
            age_s: 5,
            group: "task:t1",
            blocks: [],
            impact: 1,
            artifact_ids: [],
            failed_dependencies: [],
            task: state.task,
          },
        ],
        groups: [
          {
            key: "task:t1",
            project: "bare",
            subject_type: "task",
            subject_id: "t1",
            title: "Fix login",
            open_decisions: 0,
            oldest_age_s: 5,
            stalled_tasks: 1,
          },
        ],
        total: 1,
        has_more: false,
      });
    }
    return json([]);
  }) as typeof fetch;
  return state;
});

vi.mock("../live", async () => {
  const { useCallback, useEffect, useState } = await import("react");
  return {
    reportPage: () => {},
    useLiveData: <T,>(load: (s: AbortSignal) => Promise<T>, deps: unknown[]) => {
      const [data, setData] = useState<T>();
      const [error, setError] = useState<Error>();
      const [nonce, setNonce] = useState(0);
      useEffect(() => {
        load(new AbortController().signal).then(
          (d) => {
            setError(undefined);
            setData(d);
          },
          (e) => setError(e),
        );
      }, [...deps, nonce]);
      const reload = useCallback(() => setNonce((n) => n + 1), []);
      return { data, error, reload };
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
vi.mock("./TaskSteers", () => ({ TaskSteers: () => null }));
vi.mock("./AttemptStrip", () => ({ AttemptStrip: () => null }));
vi.mock("../delivery", () => ({ TaskDelivery: () => null }));
vi.mock("../failover", () => ({ TaskFailover: () => null }));

const URL_ = "https://git.example/acme/new";
const blocked = {
  id: "t1",
  title: "Fix login",
  project: "bare",
  state: "blocked",
  block_reason: "env_repository_unconfigured",
  kind: "generic",
  difficulty: "standard",
  difficulty_source: "heuristic",
  knowledge: [],
  attempt: 1,
  failures: 0,
  max_attempts: 3,
  required_capabilities: ["repository:write"],
  depends_on: [],
  created_at: "2026-10-20T12:00:00Z",
  created_by: "test",
  editable_fields: [],
  spec: "",
  result: null,
};

beforeEach(() => {
  net.admin = true;
  net.patches = 0;
  net.unblocks = 0;
  net.unblockStatus = 200;
  net.task = { ...blocked };
  net.project = { slug: "bare", name: "Bare", description: "d", repository_url: null };
  net.stats = [{ slug: "bare", repository_url: null, repository_source: null }];
});
afterEach(cleanup);

/** The drawer sits in a page that tracks whether it is still open. */
function Drawer() {
  const [open, setOpen] = useState(true);
  return open ? (
    <TaskDetail taskId="t1" variant="drawer" onClose={() => setOpen(false)} />
  ) : (
    <p>drawer closed</p>
  );
}

describe("blocked task in the task drawer", () => {
  it("Escape and Cancel close the repository dialog, never the drawer", async () => {
    const user = userEvent.setup();
    render(<Drawer />);
    await user.click(await screen.findByRole("button", { name: /set repository/i }));
    await screen.findByRole("dialog", { name: "Project repository" });
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText("drawer closed")).toBeNull();
    expect(screen.getByRole("button", { name: /set repository/i })).toBeTruthy();

    await user.click(screen.getByRole("button", { name: /set repository/i }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel" }),
    );
    expect(screen.queryByText("drawer closed")).toBeNull();
    // With no dialog open, Escape inside the drawer still closes it as before.
    screen.getByRole("button", { name: /set repository/i }).focus();
    await user.keyboard("{Escape}");
    expect(await screen.findByText("drawer closed")).toBeTruthy();
  });

  it("saves, unblocks once, and offers nothing more after it succeeds", async () => {
    const user = userEvent.setup();
    render(<Drawer />);
    await user.click(await screen.findByRole("button", { name: /set repository/i }));
    const dialog = await screen.findByRole("dialog");
    await user.type(await within(dialog).findByLabelText("Repository URL"), URL_);
    await user.click(within(dialog).getByRole("button", { name: "Save repository" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(net.unblocks).toBe(0);
    const unblock = await screen.findAllByRole("button", { name: "Unblock task" });
    await user.dblClick(unblock[0] as HTMLElement);
    await waitFor(() => expect(net.unblocks).toBeGreaterThan(0));
    expect(net.unblocks).toBe(1); // single-flight: the second click did nothing
    // The drawer re-reads the task: it is ready now, so no repository controls remain.
    await waitFor(() => expect(screen.queryByRole("button", { name: "Unblock task" })).toBeNull());
    expect(screen.queryByRole("button", { name: /repository/i })).toBeNull();
    expect(net.unblocks).toBe(1);
  });

  it("when the repository was set elsewhere, offers a safe unblock without a save", async () => {
    net.project.repository_url = URL_;
    net.stats = [{ slug: "bare", repository_url: URL_, repository_source: "project" }];
    const user = userEvent.setup();
    render(<Drawer />);
    expect(await screen.findByText(/now has a repository/)).toBeTruthy();
    const unblock = (await screen.findAllByRole("button", { name: "Unblock task" }))[0];
    await user.click(unblock as HTMLElement);
    await waitFor(() => expect(net.unblocks).toBe(1));
    expect(net.patches).toBe(0);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Unblock task" })).toBeNull());
  });

  it("treats a task someone else already unblocked as done, not as an error", async () => {
    net.stats = [{ slug: "bare", repository_url: URL_, repository_source: "pools" }];
    net.unblockStatus = 409;
    const user = userEvent.setup();
    render(<Drawer />);
    const unblock = (await screen.findAllByRole("button", { name: "Unblock task" }))[0];
    await user.click(unblock as HTMLElement);
    expect(await screen.findByText(/already unblocked/)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps the repository dialog open with an explanation for a non-admin", async () => {
    net.admin = false;
    const user = userEvent.setup();
    render(<Drawer />);
    await user.click(await screen.findByRole("button", { name: /set repository/i }));
    const dialog = await screen.findByRole("dialog");
    await user.type(await within(dialog).findByLabelText("Repository URL"), URL_);
    await user.click(within(dialog).getByRole("button", { name: "Save repository" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/Only an admin/);
    expect(net.patches).toBe(0);
    expect(screen.queryByRole("button", { name: "Unblock task" })).toBeNull();
  });

  it("says conflicting pools need a project repository, and offers no unblock yet", async () => {
    net.stats = [{ slug: "bare", repository_url: null, repository_source: "conflict" }];
    render(<Drawer />);
    expect(await screen.findByText(/pools name different repositories/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Unblock task" })).toBeNull();
  });
});

describe("blocked task in the inbox", () => {
  it("offers the same actions on the card, and Escape in its dialog keeps the card", async () => {
    net.stats = [{ slug: "bare", repository_url: URL_, repository_source: "project" }];
    const user = userEvent.setup();
    render(<Inbox />);
    expect(await screen.findByText(/now has a repository/)).toBeTruthy();
    await user.click(screen.getByRole("button", { name: "Change repository" }));
    await screen.findByRole("dialog", { name: "Project repository" });
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Unblock task" }));
    await waitFor(() => expect(net.unblocks).toBe(1));
    expect(await screen.findByText(/Unblocked/)).toBeTruthy();
  });
});
