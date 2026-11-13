// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Task } from "../api";
import { LiveProvider } from "../live";
import { NewTaskIntegrations, TaskIntegrations } from "./TaskIntegrations";

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

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
const kube = (id: string, name: string, state = "active") => ({
  id,
  name,
  state,
  kind: "kubernetes",
  project: "alpha",
  access: "read_only",
  validation: "ok",
  expires_at: null as string | null,
  config: { server: "https://cluster.example", namespace: "team-a" } as Record<string, unknown>,
  tool_policy: null as string | null,
});
const mcp = (id: string, name: string, tools: string[] | null, tool_policy: string) => ({
  ...kube(id, name),
  kind: "mcp",
  config: { url: "https://mcp.example/api", ...(tools ? { tools } : {}) },
  tool_policy,
});
let puts: unknown[];

beforeEach(() => {
  puts = [];
  mock.fetch.mockReset().mockImplementation(async (request: Request) => {
    const path = new URL(request.url).pathname;
    if (path === "/api/projects/alpha/integrations")
      return json([
        kube("k1", "staging"),
        kube("k2", "prod"),
        kube("k3", "gone", "revoked"),
        { ...kube("k4", "fresh"), validation: "unchecked" },
        { ...kube("k5", "stale"), expires_at: "2020-01-01T00:00:00Z" },
        mcp("m1", "docs", ["search", "fetch"], "allowlist"),
        mcp("m2", "wiki", null, "unknown"),
      ]);
    if (request.method === "PUT" && path === "/api/tasks/t1/integrations") {
      puts.push(await request.json());
      return json({});
    }
    throw new Error(`Unexpected test request: ${request.method} ${path}`);
  });
});
afterEach(cleanup);
afterAll(() => vi.unstubAllGlobals());

function mount(overrides: Partial<Task>, onSaved = vi.fn()) {
  const task = {
    id: "t1",
    project: "alpha",
    state: "ready",
    version: "v1",
    archived_at: null,
    integrations: ["k1"],
    ...overrides,
  } as Task;
  render(
    <LiveProvider token={() => null} onUnauthorized={() => {}}>
      <TaskIntegrations task={task} onSaved={onSaved} />
    </LiveProvider>,
  );
  return onSaved;
}

describe("TaskIntegrations", () => {
  it("offers active connections and saves the selection at the task's version", async () => {
    const onSaved = mount({});
    const user = userEvent.setup();
    const staging = (await screen.findByRole("checkbox", { name: /staging/ })) as HTMLInputElement;
    expect(staging.checked).toBe(true);
    expect(screen.queryByText(/gone/)).toBeNull();
    await user.click(screen.getByRole("checkbox", { name: /prod/ }));
    await user.click(screen.getByRole("checkbox", { name: /docs/ }));
    await user.click(screen.getByRole("button", { name: "Save integrations" }));
    expect(puts).toEqual([{ expected_version: "v1", integrations: ["k1", "k2", "m1"] }]);
    expect(onSaved).toHaveBeenCalled();
  });

  it("is read-only while an attempt runs", async () => {
    mount({ state: "claimed" });
    const staging = (await screen.findByRole("checkbox", { name: /staging/ })) as HTMLInputElement;
    expect(staging.matches(":disabled")).toBe(true);
    expect(screen.getByText(/cannot change while an attempt runs/)).toBeTruthy();
  });

  it("marks a connection that would block an attempt", async () => {
    mount({});
    expect(
      (await screen.findByRole("checkbox", { name: /fresh/ })).closest("label")?.textContent,
    ).toMatch(/not validated/);
    expect(screen.getByRole("checkbox", { name: /stale/ }).closest("label")?.textContent).toMatch(
      /expired/,
    );
    expect(screen.getByRole("checkbox", { name: /wiki/ }).closest("label")?.textContent).toMatch(
      /unusable: read only without a tool allowlist/,
    );
    expect(screen.getByRole("checkbox", { name: /docs/ }).closest("label")?.textContent).toMatch(
      /docs · MCP tools search, fetch · read only$/,
    );
    expect(
      screen.getByRole("checkbox", { name: /staging/ }).closest("label")?.textContent,
    ).not.toMatch(/expired|not validated/);
  });
});

describe("NewTaskIntegrations", () => {
  it("offers the project's active connections, none chosen by default", async () => {
    const onChange = vi.fn();
    render(
      <LiveProvider token={() => null} onUnauthorized={() => {}}>
        <NewTaskIntegrations project="alpha" chosen={[]} onChange={onChange} />
      </LiveProvider>,
    );
    const prod = (await screen.findByRole("checkbox", { name: /prod/ })) as HTMLInputElement;
    expect(prod.checked).toBe(false);
    expect(screen.queryByText(/gone/)).toBeNull();
    expect(screen.getByRole("checkbox", { name: /docs/ })).toBeTruthy();
    await userEvent.setup().click(prod);
    expect(onChange).toHaveBeenCalledWith(["k2"]);
  });
});
