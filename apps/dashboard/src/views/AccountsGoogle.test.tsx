// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { googleCredentialNotice } from "../accountProviders";
import type { Account } from "../api";
import { Accounts } from "./Accounts";

const usage = { input_tokens: 0, output_tokens: 0, cost_usd: 0 };
const account = (over: Partial<Account>): Account => ({
  id: "id-g1",
  name: "antigravity-main",
  provider: "google",
  kind: "api_key",
  secret_ref: "sops:account-id-g1",
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
    if (req === "/api/engines") {
      return {
        engines: [
          {
            id: "antigravity",
            label: "Antigravity (Google)",
            role: "executor",
            layers: [],
            axes: {
              model_provider: "google",
              model: null,
              harness: "agy -p",
              executor: "agy",
              account_provider: "google",
            },
            auth_methods: ["argus-account", "GEMINI_API_KEY", "google-oauth"],
            usable: false,
            notes: "",
          },
        ],
        local: null,
      };
    }
    const r = req as { op?: string; id: string; body: Record<string, unknown> };
    if (r.op === "post") {
      mock.posts.push(r.body);
      return {};
    }
    if (r.op === "put") {
      mock.puts.push({ id: r.id, credential: r.body.credential as Record<string, unknown> });
      return {};
    }
    return {};
  },
}));

beforeEach(() => {
  mock.list = [];
  mock.posts = [];
  mock.puts = [];
});

afterEach(cleanup);

describe("Accounts with google (Antigravity)", () => {
  it("lists a google account with its own provider mark", async () => {
    mock.list = [account({})];
    render(<Accounts />);
    expect(await screen.findByText(/google · api key/)).toBeTruthy();
    expect(screen.getByRole("img", { name: "Google Antigravity" })).toBeTruthy();
  });

  it("shows the google credential notice in the add dialog and submits JSON", async () => {
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Add account" }));
    fireEvent.click(screen.getByRole("button", { name: "Other" }));
    const select = (await screen.findByLabelText("Provider")) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBeGreaterThan(1));
    fireEvent.change(select, { target: { value: "google" } });
    expect(screen.getByText(googleCredentialNotice())).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "agy-key" } });
    fireEvent.change(screen.getByRole("textbox", { name: /Credential/i }), {
      target: { value: '{"api_key":"AIzaSyFAKE_TEST"}' },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(mock.posts).toHaveLength(1));
    expect(mock.posts[0]).toMatchObject({
      name: "agy-key",
      provider: "google",
      credential: { api_key: "AIzaSyFAKE_TEST" },
    });
  });

  it("shows the google credential notice in replace credential dialog", async () => {
    mock.list = [account({ name: "agy-oauth", kind: "subscription" })];
    render(<Accounts />);
    fireEvent.click(await screen.findByRole("button", { name: "Replace credential of agy-oauth" }));
    expect(screen.getByText(googleCredentialNotice())).toBeTruthy();
  });
});
