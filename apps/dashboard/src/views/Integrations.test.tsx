// @vitest-environment jsdom
import type { components } from "@argus/api-client";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LiveProvider } from "../live";
import { Integrations } from "./Integrations";

type Integration = components["schemas"]["Integration"];
type Create = components["schemas"]["IntegrationCreate"];
type Update = components["schemas"]["IntegrationUpdate"];
const mock = vi.hoisted(() => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  return { fetch };
});
vi.mock("../api", async (original) => {
  const actual = await original<typeof import("../api")>();
  const { createArgusClient } = await import("@argus/api-client");
  return { ...actual, api: createArgusClient({ baseUrl: "http://argus.test" }) };
});
vi.mock("../sse", () => ({ followEvents: vi.fn() }));

const TOKEN = "private-bearer-value";
const CA = btoa("-----BEGIN CERTIFICATE-----\npublic-ca\n-----END CERTIFICATE-----");
const timestamp = "2026-11-12T17:00:00Z";
function connection(overrides: Partial<Integration> = {}): Integration {
  return {
    id: "mcp-1",
    project: "alpha",
    kind: "mcp",
    name: "docs",
    config: { url: "https://mcp.example/api" },
    access: "read_only",
    state: "active",
    has_secret: true,
    validation: "unchecked",
    validation_detail: null,
    validated_at: null,
    created_at: timestamp,
    created_by: "admin",
    updated_at: timestamp,
    rotated_at: null,
    revoked_at: null,
    revoked_by: null,
    expires_at: null,
    version: timestamp,
    config_version: 1,
    tool_policy: (overrides.kind ?? "mcp") === "mcp" ? "unknown" : null,
    ...overrides,
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
let items: Integration[];
let projects: { slug: string; name: string }[];
let requests: { method: string; path: string; body?: Create }[];
let patches: Update[];
let intercept: ((request: Request) => Response | Promise<Response> | undefined) | undefined;

beforeEach(() => {
  items = [];
  projects = [
    { slug: "global", name: "Global" },
    { slug: "alpha", name: "Alpha" },
    { slug: "beta", name: "Beta" },
  ];
  requests = [];
  patches = [];
  intercept = undefined;
  mock.fetch.mockReset().mockImplementation(async (request: Request) => {
    const path = new URL(request.url).pathname;
    const body =
      request.method === "POST" && request.headers.get("content-type")?.includes("json")
        ? ((await request.clone().json()) as Create)
        : undefined;
    requests.push({ method: request.method, path, body });
    if (request.method === "PATCH") patches.push((await request.clone().json()) as Update);
    const intercepted = intercept?.(request);
    if (intercepted) return intercepted;
    if (path === "/api/system")
      return json({ readiness: { checks: { secrets: { status: "ok" } } } });
    if (path === "/api/projects") return json(projects);
    const project = path.split("/")[3];
    if (request.method === "GET" && path.endsWith("/integrations"))
      return json(items.filter((item) => item.project === project));
    if (request.method === "POST" && path.endsWith("/integrations") && body) {
      const saved = connection({ ...body, id: "created", project });
      items.push(saved);
      return json({ ...saved, secret: { token: TOKEN } }, 201);
    }
    const item = items.find((item) => item.id === path.split("/")[5] && item.project === project);
    if (!item) throw new Error(`Unexpected test request: ${request.method} ${path}`);
    if (path.endsWith("/validate"))
      Object.assign(item, {
        validation: "ok",
        validation_detail: "MCP response valid; credentials not explicitly rejected.",
        validated_at: timestamp,
      });
    if (path.endsWith("/rotate"))
      Object.assign(item, {
        validation: "unchecked",
        validation_detail: null,
        validated_at: null,
        rotated_at: timestamp,
      });
    if (request.method === "PATCH") {
      const edit = patches.at(-1) as Update;
      if (edit.expected_version !== item.version)
        return json({ detail: { code: "stale_version", message: "changed" } }, 409);
      const config = edit.config ?? item.config;
      const access = edit.access ?? item.access;
      Object.assign(item, {
        config,
        access,
        config_version: item.config_version + 1,
        version: "2026-11-12T18:00:00Z",
        updated_at: "2026-11-12T18:00:00Z",
        validation: "unchecked",
        validation_detail: null,
        validated_at: null,
        tool_policy:
          item.kind !== "mcp"
            ? null
            : config.tools
              ? "allowlist"
              : access === "read_write"
                ? "all"
                : "unknown",
      });
    }
    if (path.endsWith("/revoke"))
      Object.assign(item, { state: "revoked", has_secret: false, revoked_at: timestamp });
    return json({ ...item, secret: { token: TOKEN } });
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
afterAll(() => vi.unstubAllGlobals());
function mount() {
  return render(
    <LiveProvider token={() => null} onUnauthorized={() => {}}>
      <Integrations />
    </LiveProvider>,
  );
}
async function openAdd(kind: Integration["kind"] = "mcp") {
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "Add integration" }));
  await user.selectOptions(screen.getByLabelText("Kind"), kind);
  await user.type(screen.getByLabelText("Name"), kind === "mcp" ? "docs" : "cluster");
  await user.type(
    screen.getByLabelText(kind === "mcp" ? "MCP server URL" : "API server URL"),
    "https://service.example/api",
  );
  if (kind === "kubernetes") await user.type(screen.getByLabelText("Namespace"), "tools");
  await user.type(screen.getByLabelText("Bearer token"), TOKEN);
  return user;
}
const row = (name = "docs") => within(screen.getByRole("row", { name: new RegExp(name) }));
function noSecret() {
  expect(document.body.textContent).not.toContain(TOKEN);
  expect(document.body.innerHTML).not.toContain(TOKEN);
  for (const input of document.querySelectorAll("input[type=password]"))
    expect((input as HTMLInputElement).value).toBe("");
}

describe("Integrations management", () => {
  it("loads only the selected project's connections and explains the delivery boundary", async () => {
    items = [
      connection(),
      connection({
        id: "kube",
        name: "cluster",
        kind: "kubernetes",
        config: { server: "https://cluster.example", namespace: "tools" },
        validation: "failed",
        validation_detail: "token rejected (HTTP 401)",
        validated_at: timestamp,
        expires_at: "2026-12-01T00:00:00Z",
      }),
      connection({ id: "other", project: "beta", name: "beta-only" }),
    ];
    mount();
    expect(screen.getByText("Loading projects…")).toBeTruthy();
    await screen.findByRole("row", { name: /docs/ });
    expect(row().getByText("Secret present")).toBeTruthy();
    expect(row("cluster").getByText("Validation: failed")).toBeTruthy();
    expect(row("cluster").getByText("token rejected (HTTP 401)")).toBeTruthy();
    expect(row("cluster").getByText(/Expires:/)).toBeTruthy();
    expect(row().queryByText(/Expires:/)).toBeNull();
    expect(screen.getByText(/not yet injected into task workers/)).toBeTruthy();
    expect(screen.queryByRole("option", { name: "Global" })).toBeNull();
    expect(screen.queryByText("beta-only")).toBeNull();
    await userEvent.selectOptions(screen.getByLabelText("Project"), "beta");
    await screen.findByRole("row", { name: /beta-only/ });
    expect(screen.queryByRole("row", { name: /docs/ })).toBeNull();
    expect(
      requests.filter((request) => request.path.endsWith("/integrations")).map((r) => r.path),
    ).toEqual(["/api/projects/alpha/integrations", "/api/projects/beta/integrations"]);
  });

  it.each(["mcp", "kubernetes"] as const)(
    "creates %s with the supported config and clears its password before the response",
    async (kind) => {
      const pending = deferred<Response>();
      intercept = (req) => (req.method === "POST" ? pending.promise : undefined);
      const storage = vi.spyOn(Storage.prototype, "setItem");
      const log = vi.spyOn(console, "log");
      mount();
      const user = await openAdd(kind);
      const token = screen.getByLabelText("Bearer token") as HTMLInputElement;
      expect(token.type).toBe("password");
      if (kind === "kubernetes") {
        await user.type(screen.getByLabelText("CA bundle (base64 PEM, optional)"), CA);
        await user.selectOptions(screen.getByLabelText("Access"), "read_write");
      }
      await user.click(screen.getByRole("button", { name: "Save integration" }));
      expect(token.value).toBe("");
      expect(
        (screen.getByRole("button", { name: "Save integration" }) as HTMLButtonElement).disabled,
      ).toBe(true);
      noSecret();
      const posted = requests.find((r) => r.method === "POST");
      expect(posted?.path).toBe("/api/projects/alpha/integrations");
      expect(posted?.body).toEqual({
        kind,
        name: kind === "mcp" ? "docs" : "cluster",
        access: kind === "mcp" ? "read_only" : "read_write",
        config:
          kind === "mcp"
            ? { url: "https://service.example/api" }
            : { server: "https://service.example/api", namespace: "tools", ca_data: CA },
        secret: { token: TOKEN },
      });
      const saved = connection({ ...posted?.body, project: "alpha" });
      items = [saved];
      await act(async () => pending.resolve(json({ ...saved, secret: { token: TOKEN } }, 201)));
      await screen.findByRole("row", { name: new RegExp(saved.name) });
      noSecret();
      expect(storage).not.toHaveBeenCalled();
      expect(log).not.toHaveBeenCalled();
    },
  );

  it.each(["mcp", "kubernetes"] as const)(
    "leaves no %s token after a failed save and permits a fresh retry",
    async (kind) => {
      intercept = (req) =>
        req.method === "POST"
          ? json(
              {
                detail: { code: "config_invalid", message: `rejected ${TOKEN}`, details: [TOKEN] },
              },
              422,
            )
          : undefined;
      mount();
      const user = await openAdd(kind);
      await user.click(screen.getByRole("button", { name: "Save integration" }));
      expect((await screen.findByRole("alert")).textContent).toMatch(/check/i);
      noSecret();
      expect((screen.getByLabelText("Name") as HTMLInputElement).value).toBe(
        kind === "mcp" ? "docs" : "cluster",
      );
      intercept = undefined;
      await user.type(screen.getByLabelText("Bearer token"), "fresh-token");
      await user.click(screen.getByRole("button", { name: "Save integration" }));
      await screen.findByRole("row", { name: new RegExp(kind === "mcp" ? "docs" : "cluster") });
      expect(requests.filter((r) => r.method === "POST").at(-1)?.body?.secret.token).toBe(
        "fresh-token",
      );
      noSecret();
    },
  );

  it.each(["mcp", "kubernetes"] as const)(
    "validates, rotates and confirms revocation for %s",
    async (kind) => {
      items = [
        connection({
          kind,
          project: "beta",
          config:
            kind === "mcp"
              ? { url: "https://mcp.example" }
              : { server: "https://cluster.example", namespace: "tools" },
        }),
      ];
      mount();
      const user = userEvent.setup();
      await user.selectOptions(await screen.findByLabelText("Project"), "beta");
      await user.click(await screen.findByRole("button", { name: "Validate docs" }));
      await row().findByText("Validation: ok");
      await user.click(row().getByRole("button", { name: "Rotate docs" }));
      await user.type(screen.getByLabelText("Bearer token"), TOKEN);
      await user.type(screen.getByLabelText(/Expires/), "2026-12-01T10:30");
      await user.click(screen.getByRole("button", { name: "Rotate token" }));
      await row().findByText("Validation: unchecked");
      expect(requests.find((request) => request.path.endsWith("/rotate"))?.body).toEqual({
        secret: { token: TOKEN },
        expires_at: new Date("2026-12-01T10:30").toISOString(),
      });
      noSecret();
      await user.click(row().getByRole("button", { name: "Revoke docs" }));
      const dialog = within(screen.getByRole("dialog", { name: "Revoke docs" }));
      const confirm = dialog.getByRole("button", {
        name: "Revoke integration",
      }) as HTMLButtonElement;
      expect(confirm.disabled).toBe(true);
      expect(requests.some((r) => r.path.endsWith("/revoke"))).toBe(false);
      await user.click(dialog.getByRole("button", { name: "Cancel" }));
      expect(row().getByText("active")).toBeTruthy();
      await user.click(row().getByRole("button", { name: "Revoke docs" }));
      await user.type(screen.getByLabelText("Type docs to confirm"), "docs");
      await user.click(screen.getByRole("button", { name: "Revoke integration" }));
      await row().findByText("revoked");
      expect(row().getByText("Secret absent")).toBeTruthy();
      expect(row().queryAllByRole("button")).toHaveLength(0);
      expect(requests.filter((r) => r.method === "POST").map((r) => r.path)).toEqual([
        "/api/projects/beta/integrations/mcp-1/validate",
        "/api/projects/beta/integrations/mcp-1/rotate",
        "/api/projects/beta/integrations/mcp-1/revoke",
      ]);
      noSecret();
    },
  );

  it("edits the MCP endpoint and tool allowlist at the read version without a token", async () => {
    items = [connection({ validation: "ok", validated_at: timestamp })];
    mount();
    const user = userEvent.setup();
    await screen.findByRole("row", { name: /docs/ });
    expect(row().getByText(/unknown coverage; unusable by workers/)).toBeTruthy();
    await user.click(row().getByRole("button", { name: "Edit docs" }));
    const dialog = within(screen.getByRole("dialog", { name: "Edit docs" }));
    expect(document.querySelector("input[type=password]")).toBeNull();
    expect(dialog.getByText(/resets validation/)).toBeTruthy();
    const url = dialog.getByLabelText("MCP server URL") as HTMLInputElement;
    expect(url.value).toBe("https://mcp.example/api");
    await user.clear(url);
    await user.type(url, "https://mcp-2.example/api");
    await user.type(dialog.getByLabelText(/Tool allowlist/), "search\n fetch \n\n");
    await user.click(dialog.getByRole("button", { name: "Save changes" }));
    await row().findByText("Tool allowlist: 2 tools");
    expect(patches).toEqual([
      {
        expected_version: timestamp,
        config: { url: "https://mcp-2.example/api", tools: ["search", "fetch"] },
        access: "read_only",
      },
    ]);
    expect(requests.find((r) => r.method === "PATCH")?.path).toBe(
      "/api/projects/alpha/integrations/mcp-1",
    );
    expect(row().getByText("Validation: unchecked")).toBeTruthy();
    expect(row().getByText("https://mcp-2.example/api")).toBeTruthy();
    // Clearing the list removes the allowlist; read and write then allows every tool.
    await user.click(row().getByRole("button", { name: "Edit docs" }));
    const again = within(screen.getByRole("dialog", { name: "Edit docs" }));
    expect((again.getByLabelText(/Tool allowlist/) as HTMLTextAreaElement).value).toBe(
      "search\nfetch",
    );
    await user.clear(again.getByLabelText(/Tool allowlist/));
    await user.selectOptions(again.getByLabelText("Access"), "read_write");
    await user.click(again.getByRole("button", { name: "Save changes" }));
    await row().findByText("Tools: all tools the server lists");
    expect(patches.at(-1)).toEqual({
      expected_version: "2026-11-12T18:00:00Z",
      config: { url: "https://mcp-2.example/api" },
      access: "read_write",
    });
    noSecret();
  });

  it("edits a Kubernetes target and keeps its CA bundle", async () => {
    items = [
      connection({
        kind: "kubernetes",
        config: { server: "https://cluster.example", namespace: "tools", ca_data: CA },
      }),
    ];
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit docs" }));
    const dialog = within(screen.getByRole("dialog", { name: "Edit docs" }));
    expect(dialog.queryByLabelText(/Tool allowlist/)).toBeNull();
    await user.clear(dialog.getByLabelText("Namespace"));
    await user.type(dialog.getByLabelText("Namespace"), "team-b");
    await user.click(dialog.getByRole("button", { name: "Save changes" }));
    await row().findByText("Target namespace: team-b");
    expect(patches).toEqual([
      {
        expected_version: timestamp,
        config: { server: "https://cluster.example", namespace: "team-b", ca_data: CA },
        access: "read_only",
      },
    ]);
  });

  it.each([
    [409, "stale_version", /changed this connection since you opened it/],
    [409, "integration_revoked", /was revoked/],
    [422, "config_invalid", /tool names/],
  ])("shows a fixed message for a %i %s edit", async (status, code, message) => {
    items = [connection()];
    intercept = (req) =>
      req.method === "PATCH"
        ? json({ detail: { code, message: `bad ${TOKEN}`, details: [TOKEN] } }, status)
        : undefined;
    mount();
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Edit docs" }));
    const dialog = within(screen.getByRole("dialog", { name: "Edit docs" }));
    await user.click(dialog.getByRole("button", { name: "Save changes" }));
    expect((await dialog.findByRole("alert")).textContent).toMatch(message);
    expect(row().getByText("Validation: unchecked")).toBeTruthy();
    noSecret();
  });

  it("keeps a revoked connection disabled when an older list response arrives", async () => {
    items = [connection()];
    mount();
    const user = userEvent.setup();
    await screen.findByRole("button", { name: "Revoke docs" });
    const staleList = deferred<Response>();
    intercept = (request) =>
      request.method === "GET" && request.url.endsWith("/integrations")
        ? staleList.promise
        : undefined;
    await user.click(row().getByRole("button", { name: "Revoke docs" }));
    await user.type(screen.getByLabelText("Type docs to confirm"), "docs");
    await user.click(screen.getByRole("button", { name: "Revoke integration" }));
    await row().findByText("revoked");
    await act(async () => staleList.resolve(json([connection()])));
    expect(row().getByText("revoked")).toBeTruthy();
    expect(row().getByText("Secret absent")).toBeTruthy();
    expect(row().queryAllByRole("button")).toHaveLength(0);
  });

  it("supports keyboard cancellation and discards an unfinished token", async () => {
    mount();
    const user = await openAdd();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Add integration" }));
    await user.click(screen.getByRole("button", { name: "Add integration" }));
    expect(document.activeElement).toBe(screen.getByRole("dialog"));
    await user.tab({ shift: true });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Save integration" }));
    await user.tab();
    expect(document.activeElement).toBe(screen.getByLabelText("Kind"));
    noSecret();
  });

  it("reports unavailable secret storage without showing readiness details", async () => {
    intercept = (request) =>
      request.url.endsWith("/system")
        ? json({ readiness: { checks: { secrets: { status: "degraded", detail: TOKEN } } } })
        : undefined;
    mount();
    await screen.findByText("not loaded");
    noSecret();
  });

  it.each(["mcp", "kubernetes"] as const)(
    "handles %s action errors without reflecting server secrets and clears failed rotation",
    async (kind) => {
      items = [
        connection({
          kind,
          config:
            kind === "mcp"
              ? { url: "https://mcp.example" }
              : { server: "https://cluster.example", namespace: "tools" },
        }),
      ];
      mount();
      const user = userEvent.setup();
      await screen.findByRole("button", { name: "Validate docs" });
      intercept = (req) => (req.method === "POST" ? json({ detail: TOKEN }, 503) : undefined);
      await user.click(row().getByRole("button", { name: "Validate docs" }));
      await screen.findByRole("alert");
      noSecret();
      await user.click(row().getByRole("button", { name: "Rotate docs" }));
      await user.type(screen.getByLabelText("Bearer token"), TOKEN);
      await user.click(screen.getByRole("button", { name: "Rotate token" }));
      const dialog = within(screen.getByRole("dialog", { name: "Rotate docs" }));
      await dialog.findByRole("alert");
      noSecret();
      await user.click(dialog.getByRole("button", { name: "Cancel" }));
      await user.click(row().getByRole("button", { name: "Revoke docs" }));
      await user.type(screen.getByLabelText("Type docs to confirm"), "docs");
      await user.click(screen.getByRole("button", { name: "Revoke integration" }));
      await within(screen.getByRole("dialog", { name: "Revoke docs" })).findByRole("alert");
      expect(row().getByText("active")).toBeTruthy();
      noSecret();
    },
  );

  it("shows list errors with retry, loading and empty states", async () => {
    const pending = deferred<Response>();
    intercept = (req) => (req.url.endsWith("/integrations") ? pending.promise : undefined);
    mount();
    await screen.findByText("Loading integrations…");
    await act(async () => pending.resolve(json({ detail: TOKEN }, 403)));
    expect((await screen.findByRole("alert")).textContent).toMatch(/admin/i);
    noSecret();
    intercept = undefined;
    await userEvent.click(screen.getByRole("button", { name: "Retry integrations" }));
    await screen.findByText("No integrations for this project.");
  });

  it("reports project errors, retries, and excludes the global scope when no project exists", async () => {
    intercept = (req) => (req.url.endsWith("/projects") ? json({ detail: TOKEN }, 500) : undefined);
    mount();
    await screen.findByRole("alert");
    noSecret();
    projects = [{ slug: "global", name: "Global" }];
    intercept = undefined;
    await userEvent.click(screen.getByRole("button", { name: "Retry projects" }));
    await screen.findByText("Create a project before adding integrations.");
    expect(screen.queryByRole("button", { name: "Add integration" })).toBeNull();
  });

  it("clears an unfinished secret when changing projects and scopes subsequent creation", async () => {
    mount();
    const user = await openAdd("kubernetes");
    fireEvent.change(screen.getByLabelText("Project"), { target: { value: "beta" } });
    await screen.findByText("No integrations for this project.");
    expect(screen.queryByRole("dialog")).toBeNull();
    noSecret();
    await user.click(screen.getByRole("button", { name: "Add integration" }));
    expect((screen.getByLabelText("Bearer token") as HTMLInputElement).value).toBe("");
    await user.type(screen.getByLabelText("Name"), "beta-docs");
    await user.type(screen.getByLabelText("MCP server URL"), "https://beta.example/api");
    await user.type(screen.getByLabelText("Bearer token"), "beta-token");
    await user.click(screen.getByRole("button", { name: "Save integration" }));
    await screen.findByRole("row", { name: /beta-docs/ });
    expect(requests.find((r) => r.method === "POST")?.path).toBe("/api/projects/beta/integrations");
  });
});
