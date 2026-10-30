// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { copilotTokenNotice, validateCopilotToken, withoutSecret } from "../accountProviders";
import type { Account } from "../api";
import { Accounts } from "./Accounts";

const usage = { input_tokens: 0, output_tokens: 0, cost_usd: 0 };
const account = (over: Partial<Account>): Account => ({
  id: "id-1",
  name: "seat",
  provider: "copilot",
  kind: "subscription",
  secret_ref: "sops:account-id-1",
  priority: 1,
  max_leases: null,
  state: "available",
  exhausted_until: null,
  disabled_reason: null,
  labels: {},
  active_leases: 0,
  usage_24h: usage,
  usage_total: usage,
  quota_status: { state: "unknown", reason: "no_provider_source", windows: [] },
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  ...over,
});

const mock = vi.hoisted(() => ({
  list: [] as unknown[],
  posts: [] as Record<string, unknown>[],
  puts: [] as { id: string; credential: Record<string, unknown> }[],
  failWith: undefined as Error | undefined,
}));

vi.mock("../live", async () => {
  const { useCallback, useEffect, useState } = await import("react");
  return {
    useLiveData: <T,>(load: () => Promise<T>) => {
      const [data, setData] = useState<T>();
      const reload = useCallback(() => load().then(setData), [load]);
      useEffect(() => {
        reload();
      }, []);
      return { data, error: undefined, reload };
    },
  };
});

vi.mock("../api", () => ({
  api: {
    GET: (path: string) => path,
    POST: (path: string, init?: { body?: Record<string, unknown> }) => ({
      op: "post",
      path,
      body: init?.body,
    }),
    PATCH: () => "patch",
    PUT: (_: string, init: { params: { path: { account_id: string } }; body: object }) => ({
      op: "put",
      id: init.params.path.account_id,
      body: init.body,
    }),
  },
  call: async (req: unknown) => {
    if (req === "/api/accounts") return mock.list;
    if (req === "/api/accounts/usage") return { accounts: [] };
    if (req === "/api/engines") return { engines: [], local: null };
    const r = req as { op?: string; id: string; body: Record<string, unknown> };
    if (mock.failWith) throw mock.failWith;
    if (r.op === "post") {
      mock.posts.push(r.body);
      return {};
    }
    if (r.op === "put") {
      mock.puts.push({ id: r.id, credential: r.body.credential as Record<string, unknown> });
      // A redacted server response: nothing here may reach the screen.
      return { id: r.id, credential: { token: "***", leaked: "server-secret-value" } };
    }
    return [];
  },
}));

beforeEach(() => {
  mock.list = [account({})];
});

afterEach(() => {
  cleanup();
  mock.posts = [];
  mock.puts = [];
  mock.failWith = undefined;
});

const TOKEN = "github_pat_11ABCDEFG_secretvalue";

async function openAdd() {
  render(<Accounts />);
  fireEvent.click(screen.getByRole("button", { name: "Add account" }));
  await screen.findByLabelText("Provider");
  fireEvent.change(screen.getByLabelText("Name"), { target: { value: "seat" } });
  fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "copilot" } });
}

describe("copilot token hint", () => {
  it.each(["github_pat_abc", "gho_abc", "ghu_abc", "  gho_abc  "])("accepts %s", (t) => {
    expect(validateCopilotToken(t)).toBeNull();
  });

  it("rejects classic and unknown tokens naming the rule, not the value", () => {
    const classic = validateCopilotToken("ghp_supersecret");
    expect(classic).toMatch(/ghp_/);
    expect(classic).not.toContain("supersecret");
    const unknown = validateCopilotToken("xyz-supersecret");
    expect(unknown).toMatch(/github_pat_, gho_ or ghu_/);
    expect(unknown).not.toContain("supersecret");
    expect(validateCopilotToken("  ")).toBeTruthy();
  });

  it("states the accepted types and the classic rejection", () => {
    const n = copilotTokenNotice();
    for (const p of ["github_pat_", "gho_", "ghu_", "ghp_"]) expect(n).toContain(p);
    expect(n).toMatch(/rejected/);
  });

  it("removes a secret from a message", () => {
    expect(withoutSecret("bad gho_x here", "gho_x")).toBe("bad [redacted] here");
  });
});

describe("Accounts with copilot", () => {
  it("lists a copilot account with its own provider mark", async () => {
    render(<Accounts />);
    expect(await screen.findByRole("img", { name: "GitHub Copilot" })).toBeTruthy();
    expect(screen.getByText(/copilot · subscription/)).toBeTruthy();
  });

  it("offers the provider with the token notice and a fixed subscription kind", async () => {
    await openAdd();
    expect(screen.getByText(/fine-grained personal access token \(github_pat_/)).toBeTruthy();
    expect(screen.getByText(/classic personal access token \(ghp_\) is rejected/i)).toBeTruthy();
    expect((screen.getByLabelText("Copilot token") as HTMLInputElement).value).toBe("");
    expect((screen.getByLabelText("Copilot token") as HTMLInputElement).type).toBe("password");
    const kind = screen.getByLabelText("Kind") as HTMLSelectElement;
    expect(kind.value).toBe("subscription");
    expect(kind.disabled).toBe(true);
    expect(screen.queryByLabelText(/Credential \(JSON/)).toBeNull();
  });

  it.each(["github_pat_abc", "gho_abc", "ghu_abc"])("creates with %s", async (token) => {
    await openAdd();
    fireEvent.change(screen.getByLabelText("Copilot token"), { target: { value: token } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(mock.posts).toHaveLength(1));
    expect(mock.posts[0]).toMatchObject({
      provider: "copilot",
      kind: "subscription",
      credential: { token },
    });
  });

  it.each([
    ["ghp_classicsecret", /are not accepted/],
    ["unknown_secretvalue", /github_pat_, gho_ or ghu_/],
  ])("rejects %s client-side without echoing it", async (token, rule) => {
    await openAdd();
    fireEvent.change(screen.getByLabelText("Copilot token"), { target: { value: token } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    const err = await screen.findByText(rule);
    expect(err.textContent).not.toContain("secretvalue");
    expect(err.textContent).not.toContain("classicsecret");
    expect(mock.posts).toHaveLength(0);
  });

  it("clears the token after a failed create and hides it from the error", async () => {
    mock.failWith = new Error(`rejected ${TOKEN}`);
    await openAdd();
    const box = screen.getByLabelText("Copilot token") as HTMLInputElement;
    fireEvent.change(box, { target: { value: TOKEN } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await screen.findByText("rejected [redacted]");
    expect(box.value).toBe("");
    expect(document.body.textContent).not.toContain(TOKEN);
  });

  it("shows the safe error for an unauthorized (403) import", async () => {
    mock.failWith = new Error("admin token required");
    await openAdd();
    fireEvent.change(screen.getByLabelText("Copilot token"), { target: { value: TOKEN } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText("admin token required")).toBeTruthy();
    expect(document.body.textContent).not.toContain(TOKEN);
  });

  it("replaces from a blank form, sends the token and shows no response data", async () => {
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Replace credential of seat" }));
    const box = screen.getByLabelText("Copilot token") as HTMLInputElement;
    expect(box.value).toBe("");
    expect(screen.queryByLabelText("New credential")).toBeNull();
    expect(screen.getByText(/classic personal access token \(ghp_\) is rejected/i)).toBeTruthy();
    fireEvent.change(box, { target: { value: TOKEN } });
    fireEvent.click(screen.getByRole("button", { name: "Replace credential" }));
    await waitFor(() => expect(mock.puts).toEqual([{ id: "id-1", credential: { token: TOKEN } }]));
    expect(await screen.findByText("Replaced the credential of “seat”.")).toBeTruthy();
    expect(document.body.textContent).not.toContain(TOKEN);
    expect(document.body.textContent).not.toContain("server-secret-value");
  });

  it("refuses a classic token on replace and clears the field after a 403", async () => {
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Replace credential of seat" }));
    const box = screen.getByLabelText("Copilot token") as HTMLInputElement;
    fireEvent.change(box, { target: { value: "ghp_classicsecret" } });
    fireEvent.click(screen.getByRole("button", { name: "Replace credential" }));
    expect((await screen.findByRole("alert")).textContent).not.toContain("classicsecret");
    expect(mock.puts).toHaveLength(0);

    mock.failWith = new Error("admin token required");
    fireEvent.change(box, { target: { value: TOKEN } });
    fireEvent.click(screen.getByRole("button", { name: "Replace credential" }));
    expect(await screen.findByText("admin token required")).toBeTruthy();
    expect(box.value).toBe("");
    expect(document.body.textContent).not.toContain(TOKEN);
  });

  it("shows only redacted credential state in the account detail", async () => {
    render(<Accounts />);
    await screen.findByRole("img", { name: "GitHub Copilot" });
    fireEvent.click(document.querySelector(".account-name") as HTMLElement);
    expect(await screen.findByText(/Credential: stored \(write-only, never shown\)/)).toBeTruthy();
  });

  it("marks the import dialog for the responsive token layout", async () => {
    await openAdd();
    const form = screen.getByLabelText("Copilot token").closest("form");
    expect(form?.classList.contains("dialog")).toBe(true);
    expect(form?.classList.contains("copilot-import")).toBe(true);
  });
});
