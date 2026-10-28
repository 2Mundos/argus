// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Account } from "../api";
import { Accounts } from "./Accounts";

const usage = { input_tokens: 0, output_tokens: 0, cost_usd: 0 };
const account = (over: Partial<Account>): Account => ({
  id: "id-1",
  name: "work",
  provider: "acme",
  kind: "api_key",
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
  quota_status: { state: "unknown", reason: "no_usage_records", windows: [] },
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  ...over,
});

const mock = vi.hoisted(() => ({
  list: [] as unknown[],
  listCalls: 0,
  patches: [] as { id: string; body: Record<string, unknown> }[],
  deletes: [] as { id: string; confirm: string }[],
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
    GET: (path: string) => (path === "/api/accounts/usage" ? "usage" : "list"),
    POST: () => "post",
    PATCH: (_: string, init: { params: { path: { account_id: string } }; body: object }) => ({
      op: "patch",
      id: init.params.path.account_id,
      body: init.body,
    }),
    PUT: (
      _: string,
      init: { params: { path: { account_id: string } }; body: { credential: object } },
    ) => ({ op: "put", id: init.params.path.account_id, credential: init.body.credential }),
    DELETE: (
      _: string,
      init: { params: { path: { account_id: string }; query: { confirm_name: string } } },
    ) => ({
      op: "delete",
      id: init.params.path.account_id,
      confirm: init.params.query.confirm_name,
    }),
  },
  call: async (req: unknown) => {
    if (req === "usage") return { accounts: [] };
    if (req === "list") {
      mock.listCalls += 1;
      return mock.list;
    }
    const r = req as {
      op: string;
      id: string;
      body?: Record<string, unknown>;
      confirm?: string;
      credential?: Record<string, unknown>;
    };
    if (mock.failWith) throw mock.failWith;
    if (r.op === "patch") mock.patches.push({ id: r.id, body: r.body ?? {} });
    if (r.op === "put") mock.puts.push({ id: r.id, credential: r.credential ?? {} });
    if (r.op === "delete") {
      mock.deletes.push({ id: r.id, confirm: r.confirm ?? "" });
      mock.list = mock.list.filter((a) => (a as Account).id !== r.id);
    }
    return {};
  },
}));

beforeEach(() => {
  mock.list = [
    account({}),
    account({ id: "id-2", name: "off", state: "disabled", disabled_reason: "by me" }),
  ];
  mock.listCalls = 0;
  mock.patches = [];
  mock.deletes = [];
  mock.puts = [];
  mock.failWith = undefined;
});
afterEach(cleanup);

describe("account row actions", () => {
  it("labels every action with the account it targets", async () => {
    render(<Accounts />);
    for (const name of ["work", "off"]) {
      expect(await screen.findByRole("button", { name: `Edit ${name}` })).toBeTruthy();
      expect(screen.getByRole("button", { name: `Delete ${name}` })).toBeTruthy();
    }
    expect(screen.getByRole("button", { name: "Disable work" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Enable off" })).toBeTruthy();
  });

  it("edits only changed fields, shows no credential, and refreshes the list", async () => {
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit work" }));
    expect(document.querySelector(".dialog textarea, .dialog input[type=password]")).toBeNull();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "work2" } });
    fireEvent.change(screen.getByLabelText(/Max concurrent leases/), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("checkbox"));
    const before = mock.listCalls;
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mock.patches).toHaveLength(1));
    expect(mock.patches[0]).toEqual({
      id: "id-1",
      body: { name: "work2", max_leases: 3, enabled: false },
    });
    await waitFor(() => expect(mock.listCalls).toBeGreaterThan(before));
    expect(screen.queryByRole("form", { name: "Edit work" })).toBeNull();
    expect(screen.getByText("Saved “work2”.")).toBeTruthy();
  });

  it("edits token limits, rejects bad values and clears them with empty inputs", async () => {
    mock.list = [account({ token_limits: { "5h": 1000 } })];
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit work" }));
    fireEvent.change(screen.getByLabelText("7d"), { target: { value: "1.5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/7d token limit/);
    expect(mock.patches).toHaveLength(0);
    fireEvent.change(screen.getByLabelText("7d"), { target: { value: "5000" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mock.patches).toHaveLength(1));
    expect(mock.patches[0]?.body).toEqual({ token_limits: { "5h": 1000, "7d": 5000 } });
  });

  it("clears the lease limit back to unlimited", async () => {
    mock.list = [account({ max_leases: 2 })];
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit work" }));
    fireEvent.change(screen.getByLabelText(/Max concurrent leases/), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mock.patches[0]?.body).toEqual({ max_leases: null }));
  });

  it("keeps the dialog open with the error when saving fails, and Cancel discards", async () => {
    mock.failWith = new Error("an account named 'x' already exists");
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit work" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/already exists/);
    expect(screen.getByLabelText("Name")).toHaveProperty("value", "x");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alert")).toBeNull();
    expect(mock.patches).toHaveLength(0);
  });

  it("rejects an invalid lease limit client-side", async () => {
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit work" }));
    fireEvent.change(screen.getByLabelText(/Max concurrent leases/), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/at least 1/);
    expect(mock.patches).toHaveLength(0);
  });

  it("deletes only after the exact name is typed, then refreshes the list", async () => {
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete off" }));
    const confirm = screen.getByRole("button", { name: "Delete account" });
    expect(confirm).toHaveProperty("disabled", true);
    fireEvent.change(screen.getByLabelText("Confirm account name"), { target: { value: "wor" } });
    expect(confirm).toHaveProperty("disabled", true);
    fireEvent.change(screen.getByLabelText("Confirm account name"), { target: { value: "off" } });
    fireEvent.click(confirm);
    await waitFor(() => expect(mock.deletes).toEqual([{ id: "id-2", confirm: "off" }]));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Edit off" })).toBeNull());
    expect(screen.getByRole("button", { name: "Edit work" })).toBeTruthy();
    expect(screen.getByText("Deleted “off”.")).toBeTruthy();
  });

  it("cancelling a delete changes nothing", async () => {
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete work" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(mock.deletes).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Edit work" })).toBeTruthy();
  });

  it("shows the server's live-lease conflict and keeps the account", async () => {
    mock.failWith = new Error("account has 1 active lease(s); wait for release or disable it");
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete work" }));
    fireEvent.change(screen.getByLabelText("Confirm account name"), { target: { value: "work" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete account" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/active lease/);
    expect(mock.deletes).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Delete account" })).toHaveProperty(
      "disabled",
      false,
    );
  });

  it("offers Replace credential for non-anthropic accounts and keeps Renew for anthropic", async () => {
    mock.list = [account({}), account({ id: "id-3", name: "claude", provider: "anthropic" })];
    render(<Accounts />);
    expect(await screen.findByRole("button", { name: "Replace credential of work" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Replace credential of claude" })).toBeNull();
    expect(screen.getByRole("button", { name: "Renew credential of claude" })).toBeTruthy();
  });

  it("replaces a credential from a blank form without echoing it", async () => {
    mock.list = [account({ state: "disabled", disabled_reason: "x" })];
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Replace credential of work" }));
    const box = screen.getByLabelText("New credential") as HTMLTextAreaElement;
    expect(box.value).toBe("");
    fireEvent.change(box, { target: { value: '{"accessToken":"s3cret-value"}' } });
    fireEvent.click(screen.getByRole("button", { name: "Replace credential" }));
    await waitFor(() =>
      expect(mock.puts).toEqual([{ id: "id-1", credential: { accessToken: "s3cret-value" } }]),
    );
    expect(await screen.findByText("Replaced the credential of “work”.")).toBeTruthy();
    expect(document.body.textContent).not.toContain("s3cret-value");
    expect(mock.patches).toHaveLength(0); // nothing re-enables the account
  });

  it("loads a chosen JSON file into the form", async () => {
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Replace credential of work" }));
    const file = new File(['{"k":"v"}'], "auth.json", { type: "application/json" });
    Object.defineProperty(file, "text", { value: async () => '{"k":"v"}' });
    fireEvent.change(screen.getByLabelText("Credential file"), { target: { files: [file] } });
    await waitFor(() =>
      expect((screen.getByLabelText("New credential") as HTMLTextAreaElement).value).toBe(
        '{"k":"v"}',
      ),
    );
  });

  it("rejects non-object JSON locally and shows server errors without the value", async () => {
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Replace credential of work" }));
    fireEvent.change(screen.getByLabelText("New credential"), { target: { value: "[1]" } });
    fireEvent.click(screen.getByRole("button", { name: "Replace credential" }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/JSON object/);
    fireEvent.change(screen.getByLabelText("New credential"), { target: { value: "{}" } });
    fireEvent.click(screen.getByRole("button", { name: "Replace credential" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/empty/));
    mock.failWith = new Error("invalid acme credential: must be a non-empty JSON object");
    fireEvent.change(screen.getByLabelText("New credential"), {
      target: { value: '{"a":"zzz-secret"}' },
    });
    fireEvent.click(screen.getByRole("button", { name: "Replace credential" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toMatch(/non-empty/));
    expect(screen.getByRole("alert").textContent).not.toContain("zzz-secret");
    expect(mock.puts).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("New credential")).toBeNull();
  });

  it("uses username and token fields to replace a gitea credential", async () => {
    mock.list = [account({ provider: "gitea" })];
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Replace credential of work" }));
    expect((screen.getByLabelText("Gitea token") as HTMLInputElement).value).toBe("");
    fireEvent.change(screen.getByLabelText("Gitea username"), { target: { value: "pedro" } });
    fireEvent.change(screen.getByLabelText("Gitea token"), { target: { value: "tok" } });
    fireEvent.click(screen.getByRole("button", { name: "Replace credential" }));
    await waitFor(() =>
      expect(mock.puts[0]?.credential).toEqual({ username: "pedro", token: "tok" }),
    );
  });

  it("uses a single token field to replace a github credential", async () => {
    mock.list = [account({ provider: "github" })];
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Replace credential of work" }));
    expect(screen.queryByLabelText("Gitea username")).toBeNull();
    fireEvent.change(screen.getByLabelText("GitHub token"), { target: { value: "tok" } });
    fireEvent.click(screen.getByRole("button", { name: "Replace credential" }));
    await waitFor(() => expect(mock.puts[0]?.credential).toEqual({ token: "tok" }));
  });
});
