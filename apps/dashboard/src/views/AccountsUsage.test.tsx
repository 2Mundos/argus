// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Account } from "../api";
import { Accounts } from "./Accounts";

const usage = { input_tokens: 0, output_tokens: 0, cached_input_tokens: 0, cost_usd: 0, runs: 0 };
const account = (a: Partial<Account>): Account =>
  ({
    kind: "subscription",
    secret_ref: "sops:x",
    priority: 0,
    max_leases: null,
    state: "available",
    exhausted_until: null,
    disabled_reason: null,
    labels: {},
    active_leases: 0,
    usage_24h: usage,
    usage_total: usage,
    quota: [],
    billing_day: null,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    ...a,
  }) as Account;

const ACCOUNTS = [
  account({
    id: "c1",
    name: "codex-main",
    provider: "openai",
    quota: [
      {
        window: "300m",
        window_minutes: 300,
        used_percent: 37,
        resets_at: null,
        exhausted_until: null,
        observed_at: "2026-10-18T10:00:00Z",
        source: "polled",
      },
      {
        window: "10080m",
        window_minutes: 10_080,
        used_percent: 81,
        resets_at: null,
        exhausted_until: null,
        observed_at: "2026-10-18T10:00:00Z",
        source: "polled",
      },
    ],
  }),
  account({ id: "u1", name: "cursor-team", provider: "cursor" }),
  account({ id: "g1", name: "gitea-bot", provider: "gitea", kind: "api_key" }),
];

const bucket = (start: string, input: number) => ({
  start,
  input_tokens: input,
  output_tokens: 10,
  cached_input_tokens: 5,
  cost_usd: 0,
  runs: 1,
});

const mock = vi.hoisted(() => ({ queries: [] as Record<string, string>[], refreshed: 0 }));

vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  return {
    useLiveData: <T,>(load: () => Promise<T>, deps: unknown[]) => {
      const [data, setData] = useState<T>();
      useEffect(() => {
        load().then(setData);
      }, deps);
      return { data, error: undefined, reload: () => {} };
    },
  };
});

vi.mock("../api", () => ({
  api: {
    GET: (path: string, init?: { params?: { query?: Record<string, string> } }) => {
      if (init?.params?.query) mock.queries.push(init.params.query);
      return { path, query: init?.params?.query };
    },
    POST: (path: string) => ({ path }),
    PATCH: () => ({ path: "patch" }),
  },
  call: async (req: { path: string; query?: Record<string, string> }) => {
    if (req.path === "/api/accounts") return ACCOUNTS;
    if (req.path === "/api/accounts/usage") {
      const ids = req.query?.account_id ? [req.query.account_id] : ["c1", "u1", "g1"];
      return {
        bucket: req.query?.bucket ?? "day",
        accounts: ids.map((id) => ({
          account_id: id,
          reported: id === "c1",
          totals: {
            ...usage,
            input_tokens: 300,
            output_tokens: 30,
            cached_input_tokens: 15,
            runs: 3,
          },
          buckets: [bucket("2026-10-16T00:00:00Z", 100), bucket("2026-10-17T00:00:00Z", 200)],
        })),
      };
    }
    if (req.path === "/api/accounts/{account_id}/quota/refresh") {
      mock.refreshed += 1;
      return { status: "rate_limited", reason: null, quota: [] };
    }
    return [];
  },
}));

afterEach(() => {
  cleanup();
  mock.queries = [];
  mock.refreshed = 0;
});

describe("accounts usage", () => {
  it("shows quota left, sparklines, and what is not reported", async () => {
    render(<Accounts />);
    const codex = (await screen.findByText("codex-main")).closest("tr") as HTMLElement;
    expect(within(codex).getByText("63% left")).toBeTruthy();
    expect(within(codex).getByText("19% left")).toBeTruthy();
    expect(within(codex).getByRole("img", { name: /tokens per day/ })).toBeTruthy();
    const cursor = screen.getByText("cursor-team").closest("tr") as HTMLElement;
    expect(within(cursor).getByText("unknown")).toBeTruthy();
    expect(within(cursor).getByText("not reported")).toBeTruthy();
    const gitea = screen.getByText("gitea-bot").closest("tr") as HTMLElement;
    expect(within(gitea).queryByText("not reported")).toBeNull();
  });

  it("opens the detail with a time window filter and quota refresh", async () => {
    render(<Accounts />);
    fireEvent.click(await screen.findByText("codex-main"));
    const detail = await screen.findByRole("complementary", { name: "Account codex-main" });
    expect(await within(detail).findByText("Runs")).toBeTruthy();
    expect(within(detail).queryByText("Reported cost")).toBeNull(); // subscription
    fireEvent.click(within(detail).getByRole("button", { name: "Today" }));
    await waitFor(() =>
      expect(mock.queries.at(-1)).toMatchObject({ account_id: "c1", bucket: "hour" }),
    );
    fireEvent.click(within(detail).getByRole("button", { name: "Refresh quota" }));
    expect(await within(detail).findByText(/less than 5 minutes ago/)).toBeTruthy();
    expect(mock.refreshed).toBe(1);
  });
});
