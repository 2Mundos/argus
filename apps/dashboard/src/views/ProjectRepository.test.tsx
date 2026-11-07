// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Projects, taskCounts } from "./Projects";
import { RepositoryBlockAction } from "./RepositoryBlockAction";
import { TaskDefaultsEditor } from "./TaskDefaultsEditor";
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
    /** Stored task defaults by scope (`global` or a project slug). */
    settings: {} as Record<string, Record<string, unknown>>,
    settingWrites: [] as { method: string; scope: string; key: string; value?: unknown }[],
    refuseSetting: null as string | null,
  };
  const BUILT_IN: Record<string, unknown> = {
    deliverable: "response",
    priority: 0,
    difficulty: null,
    required_capabilities: [],
    budget_usd: null,
    max_attempts: 3,
    timeout_s: null,
  };
  const keys = Object.keys(BUILT_IN).map((f) => `task_default_${f}`);
  const listSettings = (scope: string) => ({
    scope,
    settings: keys.map((key) => {
      const own = scope !== "global" && key in (state.settings[scope] ?? {});
      const global = key in (state.settings.global ?? {});
      return {
        key,
        value: own
          ? state.settings[scope]?.[key]
          : global
            ? state.settings.global?.[key]
            : BUILT_IN[key.slice(13)],
        source: own ? "project" : global ? "global" : "default",
      };
    }),
  });
  // The server's resolution (task-defaults.spec.md 3.2), cut down to what the tests use.
  const effective = (slug: string, kind: string) => {
    const k = state.kinds.find((x) => x.name === kind) ?? {};
    const values: Record<string, unknown> = {};
    const sources: Record<string, string> = {};
    for (const field of Object.keys(BUILT_IN)) {
      const key = `task_default_${field}`;
      const kindValue =
        field === "required_capabilities"
          ? k.default_capabilities
          : field === "timeout_s"
            ? k.default_timeout_s
            : undefined;
      if (slug !== "global" && key in (state.settings[slug] ?? {})) {
        [values[field], sources[field]] = [state.settings[slug]?.[key], "project"];
      } else if (key in (state.settings.global ?? {})) {
        [values[field], sources[field]] = [state.settings.global?.[key], "global"];
      } else if (kindValue) {
        [values[field], sources[field]] = [kindValue, "kind"];
      } else {
        values[field] = BUILT_IN[field];
        sources[field] = field === "difficulty" ? "inferred" : "built_in";
      }
    }
    const caps = values.required_capabilities as string[];
    const problems =
      String(values.deliverable).startsWith("pr_") && !caps.includes("repository:write")
        ? [
            {
              code: "deliverable_capability",
              field: "required_capabilities",
              message: "a pull request deliverable needs the repository:write capability",
            },
          ]
        : [];
    return { project: slug, kind, values, sources, problems };
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
  globalThis.fetch = (async (info: Request | string, init?: RequestInit) => {
    const input = typeof info === "string" ? new Request(info, init) : info;
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
    const scoped = path.match(/^\/api\/(?:projects\/([^/]+)\/)?settings(?:\/([^/]+))?$/);
    if (scoped) {
      const scope = scoped[1] ?? "global";
      const key = scoped[2];
      if (!key) return json(listSettings(scope));
      if (state.refuseSetting === key) {
        return json(
          { detail: { code: "admin_only", message: "only an admin can change it" } },
          403,
        );
      }
      state.settings[scope] ??= {};
      if (input.method === "PUT") {
        const { value } = (await input.json()) as { value: unknown };
        state.settingWrites.push({ method: "PUT", scope, key, value });
        (state.settings[scope] as Record<string, unknown>)[key] = value;
      } else {
        state.settingWrites.push({ method: "DELETE", scope, key });
        delete state.settings[scope]?.[key];
      }
      return json({ key, value: null, source: "default" });
    }
    const defaults = path.match(/^\/api\/projects\/([^/]+)\/task-defaults$/);
    if (defaults) return json(effective(defaults[1] as string, url.searchParams.get("kind") ?? ""));
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
  net.settings = { bare: { task_default_budget_usd: 3 } };
  net.settingWrites.length = 0;
  net.refuseSetting = null;
  window.location.hash = "";
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

  it("prefills the budget from the project setting and sends a cleared one as no budget", async () => {
    const user = userEvent.setup();
    await openForm(user);
    await user.type(screen.getByLabelText("Title"), "Summarise");
    const budget = screen.getByLabelText(/^Budget \(USD/) as HTMLInputElement;
    await waitFor(() => expect(budget.value).toBe("3"));
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(net.tasks).toHaveLength(1));
    expect(net.tasks[0]).not.toHaveProperty("budget_usd");

    cleanup();
    await openForm(user);
    await user.type(screen.getByLabelText("Title"), "Unbounded");
    const typed = screen.getByLabelText(/^Budget \(USD/) as HTMLInputElement;
    await waitFor(() => expect(typed.value).toBe("3"));
    fireEvent.change(typed, { target: { value: "" } });
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(net.tasks).toHaveLength(2));
    expect(net.tasks[1]).toMatchObject({ budget_usd: null });
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
    await user.clear(screen.getByLabelText(/^Required capabilities/));
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

const hintOf = (control: HTMLElement) =>
  document.getElementById(control.getAttribute("aria-describedby") ?? "")?.textContent;

describe("new task defaults", () => {
  it("prefills the project's effective defaults and sends only what was changed", async () => {
    net.settings.has = {
      task_default_deliverable: "pr_merged",
      task_default_required_capabilities: ["repository:write"],
      task_default_priority: 5,
    };
    const user = userEvent.setup();
    await openForm(user);
    await user.selectOptions(screen.getByLabelText("Project"), "has");
    const deliverable = screen.getByLabelText("Deliverable") as HTMLSelectElement;
    await waitFor(() => expect(deliverable.value).toBe("pr_merged"));
    expect(hintOf(deliverable)).toBe("default, from project");
    const caps = screen.getByLabelText(/^Required capabilities/) as HTMLInputElement;
    expect(caps.value).toBe("repository:write");
    expect(hintOf(screen.getByLabelText("Max attempts"))).toBe("default, built-in");
    expect((screen.getByLabelText("Priority") as HTMLInputElement).value).toBe("5");

    await user.type(screen.getByLabelText("Title"), "Ship it");
    fireEvent.change(screen.getByLabelText("Max attempts"), { target: { value: "2" } });
    expect(hintOf(screen.getByLabelText("Max attempts"))).toMatch(/^changed/);
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(net.tasks).toHaveLength(1));
    const sent = net.tasks[0] as Record<string, unknown>;
    expect(sent).toMatchObject({ project: "has", title: "Ship it", max_attempts: 2 });
    for (const left of [
      "deliverable",
      "priority",
      "required_capabilities",
      "difficulty",
      "timeout_s",
      "budget_usd",
    ]) {
      expect(sent).not.toHaveProperty(left);
    }
  });

  it("sends an explicit empty list, 0 priority, inference and no timeout as values", async () => {
    net.settings.has = {
      task_default_priority: 5,
      task_default_difficulty: "hard",
      task_default_timeout_s: 600,
    };
    const user = userEvent.setup();
    await openForm(user);
    await user.selectOptions(screen.getByLabelText("Project"), "has");
    await user.selectOptions(screen.getByLabelText("Kind"), "coder");
    const caps = screen.getByLabelText(/^Required capabilities/) as HTMLInputElement;
    await waitFor(() => expect(caps.value).toBe("repository:write"));
    expect(hintOf(caps)).toBe("default, from the kind");
    expect((screen.getByLabelText(/^Timeout in minutes/) as HTMLInputElement).value).toBe("10");

    await user.type(screen.getByLabelText("Title"), "Explicit");
    await user.clear(caps);
    fireEvent.change(screen.getByLabelText("Priority"), { target: { value: "0" } });
    await user.selectOptions(screen.getByLabelText(/^Difficulty/), "");
    fireEvent.change(screen.getByLabelText(/^Timeout in minutes/), { target: { value: "" } });
    // A field put back to its default is left out again.
    fireEvent.change(screen.getByLabelText("Max attempts"), { target: { value: "9" } });
    const attempts = screen.getByLabelText("Max attempts");
    await user.click(
      within(
        document.getElementById(attempts.getAttribute("aria-describedby") ?? "") as HTMLElement,
      ).getByRole("button", { name: "Use default" }),
    );
    expect((attempts as HTMLInputElement).value).toBe("3");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(net.tasks).toHaveLength(1));
    const sent = net.tasks[0] as Record<string, unknown>;
    expect(sent).toMatchObject({
      required_capabilities: [],
      priority: 0,
      difficulty: null,
      timeout_s: null,
    });
    expect(sent).not.toHaveProperty("max_attempts");
    expect(sent).not.toHaveProperty("deliverable");
  });

  it("says when the defaults would be refused, until the creator changes them", async () => {
    net.settings.global = { task_default_deliverable: "pr_open" };
    const user = userEvent.setup();
    await openForm(user);
    await user.selectOptions(screen.getByLabelText("Project"), "has");
    expect(await screen.findByText(/Argus refuses this task: .*repository:write/)).toBeTruthy();
    expect(hintOf(screen.getByLabelText("Deliverable"))).toBe("default, from global");
    await user.selectOptions(screen.getByLabelText("Deliverable"), "response");
    expect(screen.queryByText(/Argus refuses this task/)).toBeNull();
  });
});

const group = (name: string) => screen.getByRole("group", { name: new RegExp(`^${name}`) });

async function openDefaults(slug: string) {
  window.location.hash = `#projects/${slug}/task-defaults`;
  render(<Projects />);
  return screen.findByRole("group", { name: /^Deliverable/ });
}

describe("project task defaults", () => {
  it("overrides fields, saves them, and shows them after a reload", async () => {
    const user = userEvent.setup();
    await openDefaults("has");
    expect(screen.getByRole("heading", { name: "has name" })).toBeTruthy();
    const nav = screen.getByRole("navigation", { name: "Project sections" });
    expect(
      within(nav).getByRole("link", { name: "Task defaults" }).getAttribute("aria-current"),
    ).toBe("page");
    expect(within(nav).getByRole("link", { name: "Other settings" }).getAttribute("href")).toBe(
      "#setup/settings/has",
    );
    expect(screen.getByText("No overrides: every field inherits.")).toBeTruthy();
    expect(within(group("Deliverable")).getByText(/Effective now/).textContent).toMatch(
      /A response \(summary\) built-in/,
    );

    await user.click(within(group("Deliverable")).getByRole("radio", { name: "Override" }));
    await user.selectOptions(screen.getByLabelText("Deliverable value"), "pr_merged");
    await user.click(
      within(group("Required capabilities")).getByRole("radio", { name: "Override" }),
    );
    await user.type(screen.getByLabelText("Required capabilities value"), "repository:write");
    expect(group("Deliverable").textContent).toMatch(/unsaved/);
    await user.click(screen.getByRole("button", { name: "Save task defaults" }));
    expect((await screen.findByRole("status")).textContent).toBe("Task defaults saved.");
    expect(net.settingWrites).toEqual([
      { method: "PUT", scope: "has", key: "task_default_deliverable", value: "pr_merged" },
      {
        method: "PUT",
        scope: "has",
        key: "task_default_required_capabilities",
        value: ["repository:write"],
      },
    ]);
    await waitFor(() =>
      expect(within(group("Deliverable")).getByText(/Effective now/).textContent).toMatch(
        /A pull request merged from project/,
      ),
    );
    expect(group("Deliverable").textContent).not.toMatch(/unsaved/);

    cleanup();
    await openDefaults("has");
    expect(
      (within(group("Deliverable")).getByRole("radio", { name: "Override" }) as HTMLInputElement)
        .checked,
    ).toBe(true);
    expect((screen.getByLabelText("Deliverable value") as HTMLSelectElement).value).toBe(
      "pr_merged",
    );
  });

  it("tells explicit empty values from inherit and resets to global with a delete", async () => {
    net.settings.global = { task_default_budget_usd: 5, task_default_priority: 7 };
    net.settings.has = { task_default_max_attempts: 5 };
    const user = userEvent.setup();
    await openDefaults("has");
    expect(
      within(group("Budget")).getByRole("radio", { name: /^Inherit \(global: \$5\)/ }),
    ).toBeTruthy();

    await user.click(within(group("Budget")).getByRole("radio", { name: "Override" }));
    await user.clear(screen.getByLabelText("Budget (USD) value"));
    await user.click(within(group("Priority")).getByRole("radio", { name: "Override" }));
    fireEvent.change(screen.getByLabelText("Priority value"), { target: { value: "0" } });
    await user.click(
      within(group("Required capabilities")).getByRole("radio", { name: "Override" }),
    );
    await user.click(within(group("Max attempts")).getByRole("radio", { name: /^Inherit/ }));
    await user.click(screen.getByRole("button", { name: "Save task defaults" }));
    await screen.findByRole("status");
    expect(net.settingWrites).toEqual([
      { method: "PUT", scope: "has", key: "task_default_priority", value: 0 },
      { method: "PUT", scope: "has", key: "task_default_required_capabilities", value: [] },
      { method: "PUT", scope: "has", key: "task_default_budget_usd", value: null },
      { method: "DELETE", scope: "has", key: "task_default_max_attempts" },
    ]);
    await waitFor(() =>
      expect(within(group("Budget")).getByText(/Effective now/).textContent).toMatch(
        /no budget from project/,
      ),
    );
  });

  it("blocks saving an invalid value and reports a field the server refused", async () => {
    net.refuseSetting = "task_default_priority";
    const user = userEvent.setup();
    await openDefaults("has");
    await user.click(within(group("Max attempts")).getByRole("radio", { name: "Override" }));
    const attempts = screen.getByLabelText("Max attempts value");
    fireEvent.change(attempts, { target: { value: "50" } });
    expect(attempts.getAttribute("aria-invalid")).toBe("true");
    expect(within(group("Max attempts")).getByText("Enter a number from 1 to 20.")).toBeTruthy();
    const save = screen.getByRole("button", { name: "Save task defaults" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.change(attempts, { target: { value: "4" } });

    await user.click(within(group("Priority")).getByRole("radio", { name: "Override" }));
    fireEvent.change(screen.getByLabelText("Priority value"), { target: { value: "9" } });
    await user.click(save);
    expect((await within(group("Priority")).findByRole("alert")).textContent).toMatch(
      /Not saved: only an admin can change it/,
    );
    expect(screen.getByText(/One field was not saved; the others were\./)).toBeTruthy();
    expect(net.settingWrites).toEqual([
      { method: "PUT", scope: "has", key: "task_default_max_attempts", value: 4 },
    ]);
    // The refused edit stays, unsaved, for another try.
    expect((screen.getByLabelText("Priority value") as HTMLInputElement).value).toBe("9");
    expect(group("Priority").textContent).toMatch(/unsaved/);
  });

  it("asks before a project switch drops unsaved edits and keeps the other project apart", async () => {
    net.projects.push(project("other"));
    net.settings.has = { task_default_deliverable: "pr_merged" };
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();
    await openDefaults("has");
    await user.click(within(group("Priority")).getByRole("radio", { name: "Override" }));
    fireEvent.change(screen.getByLabelText("Priority value"), { target: { value: "3" } });

    await user.selectOptions(screen.getByLabelText("Project"), "other");
    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
    expect(window.location.hash).toBe("#projects/has/task-defaults");
    expect((screen.getByLabelText("Priority value") as HTMLInputElement).value).toBe("3");

    confirm.mockReturnValue(true);
    await user.selectOptions(screen.getByLabelText("Project"), "other");
    expect(await screen.findByRole("heading", { name: "other name" })).toBeTruthy();
    await screen.findByRole("group", { name: /^Deliverable/ });
    expect(
      (within(group("Deliverable")).getByRole("radio", { name: /^Inherit/ }) as HTMLInputElement)
        .checked,
    ).toBe(true);
    expect(screen.queryByLabelText("Priority value")).toBeNull();
    expect(net.settingWrites).toEqual([]);
    confirm.mockRestore();
  });

  it("keeps the overview's repository actions and links each project from the list", async () => {
    const user = userEvent.setup();
    render(<Projects />);
    const link = await screen.findByRole("link", { name: "has name" });
    expect(link.getAttribute("href")).toBe("#projects/has");
    window.location.hash = "#projects/has";
    await screen.findByRole("navigation", { name: "Project sections" });
    const card = screen.getByText(HAS).closest("article") as HTMLElement;
    await user.click(within(card).getByRole("button", { name: "Edit repository" }));
    expect(await screen.findByRole("dialog", { name: "Project repository" })).toBeTruthy();
  });

  it("says when the project does not exist", async () => {
    window.location.hash = "#projects/nope";
    render(<Projects />);
    expect((await screen.findByRole("alert")).textContent).toMatch(/There is no project nope/);
  });
});

describe("global task defaults", () => {
  it("sets a global value and shows the problem it would cause", async () => {
    const user = userEvent.setup();
    render(<TaskDefaultsEditor project={null} />);
    await screen.findByRole("group", { name: /^Deliverable/ });
    expect(within(group("Deliverable")).getByRole("radio", { name: /^Built-in/ })).toBeTruthy();
    await user.click(within(group("Deliverable")).getByRole("radio", { name: "Set" }));
    await user.selectOptions(screen.getByLabelText("Deliverable value"), "pr_open");
    await user.click(screen.getByRole("button", { name: "Save task defaults" }));
    await screen.findByRole("status");
    expect(net.settingWrites).toEqual([
      { method: "PUT", scope: "global", key: "task_default_deliverable", value: "pr_open" },
    ]);
    expect(
      await within(group("Required capabilities")).findByText(
        /A task created with the saved defaults is refused: .*repository:write/,
      ),
    ).toBeTruthy();
  });
});
