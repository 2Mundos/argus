// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Projects, taskCounts } from "./Projects";
import { RepositoryBlockAction } from "./RepositoryBlockAction";
import { Tasks } from "./Tasks";

/** A fake Argus behind the real API client: requests keep their real shape. */
const net = vi.hoisted(() => {
  const state = {
    admin: true,
    networkDown: false,
    statsFail: false,
    projects: [] as Record<string, unknown>[],
    stats: [] as Record<string, unknown>[],
    puts: [] as { slug: string; body: Record<string, unknown> }[],
    tasks: [] as Record<string, unknown>[],
    unblocks: [] as string[],
    statsReads: 0,
    kinds: [{ name: "generic" }] as Record<string, unknown>[],
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
    const url = new URL(input.url);
    const path = url.pathname;
    if (path === "/api/projects/stats") {
      state.statsReads += 1;
      return state.statsFail
        ? json({ detail: { code: "boom", message: "stats down" } }, 500)
        : json(state.stats);
    }
    if (path === "/api/projects") return json(state.projects);
    if (path.endsWith("/repository") && input.method === "PATCH") {
      if (state.networkDown) throw new TypeError("Failed to fetch");
      const slug = path.split("/")[3] as string;
      const body = (await input.json()) as { repository_url: string | null };
      if (!state.admin) {
        return json(
          {
            detail: {
              code: "repository_admin_only",
              message: "only an admin can change a project's repository",
            },
          },
          403,
        );
      }
      const project = state.projects.find((p) => p.slug === slug);
      if (!project) return json({ detail: { code: "unknown_project", message: "nope" } }, 404);
      state.puts.push({ slug, body });
      project.repository_url = body.repository_url; // nothing else is touched
      const entry = state.stats.find((s) => s.slug === slug);
      if (entry) entry.repository_url = body.repository_url;
      return json(project);
    }
    if (path === "/api/task-kinds") return json(state.kinds);
    if (path === "/api/tasks" && input.method === "POST") {
      state.tasks.push((await input.json()) as Record<string, unknown>);
      return json({ id: "new" }, 201);
    }
    if (path === "/api/tasks") return json({ items: [], total: 0, has_more: false });
    if (path.endsWith("/unblock")) {
      state.unblocks.push(path.split("/")[3] as string);
      return json({ id: "t1", state: "ready" });
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
vi.mock("./TaskDetail", () => ({ TaskDetail: () => null }));
vi.mock("./ArchiveTasks", () => ({ ArchiveTasks: () => null }));
vi.mock("./ModelPin", () => ({ ModelPin: () => null }));
vi.mock("./AccountPin", () => ({ AccountPin: () => null }));

const project = (slug: string, repository_url: string | null = null) => ({
  slug,
  name: `${slug} name`,
  description: `${slug} description`,
  repository_url,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
});
const stat = (slug: string, repository_url: string | null, extra = {}) => ({
  slug,
  repository_url,
  repository_source: repository_url ? "project" : null,
  tasks: {},
  archived_tasks: 0,
  last_task_activity_at: null,
  knowledge_pages: 0,
  ...extra,
});

const HAS = "https://git.example/acme/has";
const NEW = "https://git.example/acme/new";

beforeEach(() => {
  net.admin = true;
  net.networkDown = false;
  net.statsFail = false;
  net.puts.length = 0;
  net.tasks.length = 0;
  net.unblocks.length = 0;
  net.statsReads = 0;
  net.kinds = [{ name: "generic" }, { name: "coder", default_capabilities: ["repository:write"] }];
  net.projects = [project("bare"), project("has", HAS), project("global")];
  net.stats = [stat("bare", null), stat("has", HAS), stat("global", null)];
});
afterEach(cleanup);

async function openForm(user: ReturnType<typeof userEvent.setup>) {
  render(<Tasks />);
  await user.click(await screen.findByRole("button", { name: /new task/i }));
  await screen.findByRole("heading", { name: "New task" });
  await screen.findByRole("option", { name: "bare name" });
}

describe("new task with a project that has no repository", () => {
  it("configures it from the form and keeps everything typed", async () => {
    const user = userEvent.setup();
    await openForm(user);
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Fix login" } });
    fireEvent.change(screen.getByLabelText(/^Spec/), { target: { value: "Details" } });
    await user.selectOptions(screen.getByLabelText("Deliverable"), "pr_open");

    const notice = await screen.findByRole("status");
    expect(notice.textContent).toMatch(/Argus will refuse to create this task until one is set/);
    await user.click(within(notice).getByRole("button", { name: /set repository/i }));
    const dialog = await screen.findByRole("dialog", { name: "Project repository" });

    // Bad input is explained and nothing is sent.
    const input = await within(dialog).findByLabelText("Repository URL");
    fireEvent.change(input, { target: { value: "http://git.example/acme/new" } });
    await user.click(within(dialog).getByRole("button", { name: "Save repository" }));
    expect(await within(dialog).findByText(/must be an HTTPS URL/)).toBeTruthy();
    fireEvent.change(input, { target: { value: "https://bot:secret@git.example/acme/new" } });
    await user.click(within(dialog).getByRole("button", { name: "Save repository" }));
    expect(await within(dialog).findByText(/Remove the credentials/)).toBeTruthy();
    expect(net.puts).toEqual([]);

    fireEvent.change(input, { target: { value: NEW } });
    await user.click(within(dialog).getByRole("button", { name: "Save repository" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    // Only the repository is sent: the server keeps name and description as they are.
    expect(net.puts).toEqual([{ slug: "bare", body: { repository_url: NEW } }]);
    expect(screen.queryByRole("status")).toBeNull();
    expect((screen.getByLabelText("Title") as HTMLInputElement).value).toBe("Fix login");
    expect((screen.getByLabelText(/^Spec/) as HTMLTextAreaElement).value).toBe("Details");
    expect((screen.getByLabelText("Deliverable") as HTMLSelectElement).value).toBe("pr_open");

    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(net.tasks).toHaveLength(1));
    expect(net.tasks[0]).toMatchObject({
      project: "bare",
      title: "Fix login",
      deliverable: "pr_open",
    });
  });

  it("does not stop a task that needs no repository", async () => {
    const user = userEvent.setup();
    await openForm(user);
    await user.type(screen.getByLabelText("Title"), "Summarise");
    const notice = await screen.findByRole("status");
    expect(notice.textContent).toMatch(/can still run/);
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(net.tasks).toHaveLength(1));
    expect(net.puts).toEqual([]);
  });

  it("cancel leaves the form and the project untouched", async () => {
    const user = userEvent.setup();
    await openForm(user);
    await user.type(screen.getByLabelText("Title"), "Keep me");
    await user.click(await within(await screen.findByRole("status")).findByRole("button"));
    const dialog = await screen.findByRole("dialog", { name: "Project repository" });
    await user.type(await within(dialog).findByLabelText("Repository URL"), NEW);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect((screen.getByLabelText("Title") as HTMLInputElement).value).toBe("Keep me");
    expect(net.puts).toEqual([]);
    expect(screen.getByRole("status")).toBeTruthy();
  });

  it("explains a refusal for a non-admin and a network failure, then recovers", async () => {
    const user = userEvent.setup();
    net.admin = false;
    await openForm(user);
    await user.type(screen.getByLabelText("Title"), "Keep me");
    await user.click(await within(await screen.findByRole("status")).findByRole("button"));
    const dialog = await screen.findByRole("dialog", { name: "Project repository" });
    await user.type(await within(dialog).findByLabelText("Repository URL"), NEW);
    await user.click(within(dialog).getByRole("button", { name: "Save repository" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(
      /Only an admin can change a project's repository/,
    );
    expect(net.puts).toEqual([]);

    net.admin = true;
    net.networkDown = true;
    await user.click(within(dialog).getByRole("button", { name: "Save repository" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/Could not reach Argus/);
    expect((within(dialog).getByLabelText("Repository URL") as HTMLInputElement).value).toBe(NEW);

    net.networkDown = false;
    await user.click(within(dialog).getByRole("button", { name: "Save repository" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(net.puts).toHaveLength(1);
    expect((screen.getByLabelText("Title") as HTMLInputElement).value).toBe("Keep me");
  });

  it("follows the selected project", async () => {
    const user = userEvent.setup();
    await openForm(user);
    expect(await screen.findByRole("status")).toBeTruthy();
    await user.selectOptions(screen.getByLabelText("Project"), "has");
    expect(screen.queryByRole("status")).toBeNull();
    await user.selectOptions(screen.getByLabelText("Project"), "bare");
    expect(screen.getByRole("status").textContent).toMatch(/bare has no repository/);
  });

  it("does not claim a repository is missing when the check failed", async () => {
    const user = userEvent.setup();
    net.statsFail = true;
    await openForm(user);
    const note = await screen.findByRole("status");
    expect(note.textContent).toMatch(/Could not check whether bare has a repository/);
    expect(screen.queryByText(/has no repository\./)).toBeNull();
    await user.type(screen.getByLabelText("Title"), "Still works");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(net.tasks).toHaveLength(1));
  });

  it("words the warning by what the task needs, including the kind's default capabilities", async () => {
    const user = userEvent.setup();
    await openForm(user);
    expect((await screen.findByRole("status")).textContent).toMatch(/can still run/);
    // The kind's default capabilities apply while the field is empty...
    await user.selectOptions(screen.getByLabelText("Kind"), "coder");
    expect(screen.getByRole("status").textContent).toMatch(/A worker will block this task/);
    // ...and typed capabilities replace them, as on the server.
    await user.type(screen.getByLabelText(/^Required capabilities/), "other");
    expect(screen.getByRole("status").textContent).toMatch(/can still run/);
    await user.clear(screen.getByLabelText(/^Required capabilities/));
    await user.type(screen.getByLabelText(/^Required capabilities/), "repository:write");
    await user.selectOptions(screen.getByLabelText("Kind"), "generic");
    expect(screen.getByRole("status").textContent).toMatch(/A worker will block this task/);
  });

  it("does not say all workers block when the pools disagree or the value is unusable", async () => {
    net.stats = [stat("bare", null, { repository_source: "conflict" })];
    const user = userEvent.setup();
    await openForm(user);
    await user.selectOptions(screen.getByLabelText("Kind"), "coder");
    const note = (await screen.findByRole("status")).textContent ?? "";
    expect(note).toMatch(/pools name different repositories/);
    expect(note).toMatch(/Workers whose pool names one may use it/);
    expect(note).not.toMatch(/A worker will block/);
    await user.selectOptions(screen.getByLabelText("Deliverable"), "branch");
    expect(screen.getByRole("status").textContent).toMatch(/refuse to create/);
  });

  it("calls an unusable stored value unusable, not absent", async () => {
    net.stats = [stat("bare", null, { repository_source: "invalid" })];
    const user = userEvent.setup();
    await openForm(user);
    expect((await screen.findByRole("status")).textContent).toMatch(
      /stored repository that cannot be used/,
    );
  });

  it("opens the dialog on what the project holds now when the stats were stale", async () => {
    net.stats = [stat("bare", null), stat("has", HAS), stat("global", null)];
    net.projects = [project("bare", NEW), project("has", HAS), project("global")];
    const user = userEvent.setup();
    await openForm(user);
    await user.click(await within(await screen.findByRole("status")).findByRole("button"));
    const dialog = await screen.findByRole("dialog", { name: "Project repository" });
    expect(
      ((await within(dialog).findByLabelText("Repository URL")) as HTMLInputElement).value,
    ).toBe(NEW);
  });

  it("saves only the repository while the name changes elsewhere", async () => {
    const user = userEvent.setup();
    await openForm(user);
    await user.click(await within(await screen.findByRole("status")).findByRole("button"));
    const dialog = await screen.findByRole("dialog", { name: "Project repository" });
    const input = await within(dialog).findByLabelText("Repository URL");
    // Another admin renames the project while this dialog is open.
    Object.assign(net.projects[0] as object, { name: "Renamed", description: "elsewhere" });
    await user.type(input, NEW);
    await user.click(within(dialog).getByRole("button", { name: "Save repository" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(net.projects[0]).toMatchObject({
      name: "Renamed",
      description: "elsewhere",
      repository_url: NEW,
    });
    expect(Object.keys(net.puts[0]?.body ?? {})).toEqual(["repository_url"]);
  });

  it("closes only the dialog on Escape and keeps the form", async () => {
    const user = userEvent.setup();
    await openForm(user);
    await user.type(screen.getByLabelText("Title"), "Keep me");
    await user.click(await within(await screen.findByRole("status")).findByRole("button"));
    await screen.findByRole("dialog", { name: "Project repository" });
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect((screen.getByLabelText("Title") as HTMLInputElement).value).toBe("Keep me");
  });

  it("treats a repository the pools supply as configured", async () => {
    net.stats = [stat("bare", "https://git.example/pool/bare", { repository_source: "pools" })];
    const user = userEvent.setup();
    await openForm(user);
    // Wait for the stats answer first: before it, the repository is unknown, not set.
    await waitFor(() => expect(net.statsReads).toBeGreaterThan(0));
    await act(async () => {});
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByText(/has no repository/)).toBeNull();
  });
});

describe("Projects page", () => {
  it("shows repositories and real counts, and says what it does not know", async () => {
    net.stats = [
      stat("bare", null),
      stat("has", HAS, {
        tasks: { ready: 2, blocked: 1 },
        archived_tasks: 4,
        last_task_activity_at: "2026-01-02T00:00:00Z",
        knowledge_pages: 3,
      }),
      stat("global", null),
    ];
    render(<Projects />);
    const has = (await screen.findByText("has name")).closest("article") as HTMLElement;
    expect(within(has).getByText(HAS)).toBeTruthy();
    expect(within(has).getByText("2 ready")).toBeTruthy();
    expect(within(has).getByText("1 blocked")).toBeTruthy();
    expect(within(has).getByText(/4 archived/)).toBeTruthy();
    expect(within(has).getByText("3")).toBeTruthy();
    const bare = screen.getByText("bare name").closest("article") as HTMLElement;
    expect(within(bare).getByText(/Not configured/)).toBeTruthy();
    expect(within(bare).getByText("no tasks yet")).toBeTruthy();
    const global = screen.getByText("global name").closest("article") as HTMLElement;
    expect(within(global).getByText(/Not applicable/)).toBeTruthy();
    expect(within(global).queryByRole("button")).toBeNull();
  });

  it("labels a pool-supplied repository and a stats outage", async () => {
    net.stats = [stat("bare", "https://git.example/pool/bare", { repository_source: "pools" })];
    const { unmount } = render(<Projects />);
    const bare = (await screen.findByText("bare name")).closest("article") as HTMLElement;
    await within(bare).findByText(/from the pools' allowlists/);
    unmount();

    net.statsFail = true;
    render(<Projects />);
    expect((await screen.findByText(/Project statistics are unavailable/)).textContent).toMatch(
      /incomplete/,
    );
    const again = screen.getByText("bare name").closest("article") as HTMLElement;
    expect(within(again).getByText(/pool allowlists not checked/)).toBeTruthy();
    expect(within(again).queryByText(/Tasks/)).toBeNull();
  });

  it("edits, then clears, a repository and reports a refusal", async () => {
    const user = userEvent.setup();
    render(<Projects />);
    const has = (await screen.findByText("has name")).closest("article") as HTMLElement;
    await user.click(within(has).getByRole("button", { name: "Edit repository" }));
    const dialog = await screen.findByRole("dialog", { name: "Project repository" });
    const input = (await within(dialog).findByLabelText("Repository URL")) as HTMLInputElement;
    expect(input.value).toBe(HAS);

    net.admin = false;
    await user.clear(input);
    await user.type(input, NEW);
    await user.click(within(dialog).getByRole("button", { name: "Save repository" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/Only an admin/);
    expect(net.puts).toEqual([]);

    net.admin = true;
    await user.click(within(dialog).getByRole("button", { name: "Clear repository" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(net.puts).toEqual([{ slug: "has", body: { repository_url: null } }]);
    expect((await screen.findByRole("status")).textContent).toMatch(/cleared/);
  });

  it("reports a project that vanished", async () => {
    const user = userEvent.setup();
    render(<Projects />);
    await user.click(
      within((await screen.findByText("bare name")).closest("article") as HTMLElement).getByRole(
        "button",
        { name: "Set repository" },
      ),
    );
    net.projects = [];
    const dialog = await screen.findByRole("dialog", { name: "Project repository" });
    await within(dialog).findByLabelText("Repository URL");
    await user.type(within(dialog).getByLabelText("Repository URL"), NEW);
    await user.click(within(dialog).getByRole("button", { name: "Save repository" }));
    expect((await within(dialog).findByRole("alert")).textContent).toMatch(/no longer exists/);
  });

  it("says when pools conflict or the stored value is unusable", async () => {
    net.projects = [project("bare"), project("has", "https://git.example/a/.git")];
    net.stats = [
      stat("bare", null, { repository_source: "conflict" }),
      stat("has", null, { repository_source: "invalid" }),
    ];
    render(<Projects />);
    const bare = (await screen.findByText("bare name")).closest("article") as HTMLElement;
    await within(bare).findByText(/pools name different repositories/);
    const has = screen.getByText("has name").closest("article") as HTMLElement;
    expect(within(has).getByText(/stored but cannot be used/)).toBeTruthy();
    expect(within(has).getByText("https://git.example/a/.git")).toBeTruthy();
  });

  it("orders task counts and keeps unknown states", () => {
    expect(taskCounts({ failed: 1, ready: 2, odd: 3, blocked: 0 })).toEqual([
      ["ready", 2],
      ["failed", 1],
      ["odd", 3],
    ]);
  });
});

describe("blocked task without a repository", () => {
  it("sets the repository, then unblocks only when asked", async () => {
    const user = userEvent.setup();
    render(<RepositoryBlockAction taskId="t1" project="bare" />);
    expect(screen.queryByRole("button", { name: "Unblock task" })).toBeNull();
    await user.click(screen.getByRole("button", { name: /set repository/i }));
    const dialog = await screen.findByRole("dialog", { name: "Project repository" });
    await user.type(await within(dialog).findByLabelText("Repository URL"), NEW);
    await user.click(within(dialog).getByRole("button", { name: "Save repository" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(net.unblocks).toEqual([]);
    await user.click(await screen.findByRole("button", { name: "Unblock task" }));
    await waitFor(() => expect(net.unblocks).toEqual(["t1"]));
  });
});
