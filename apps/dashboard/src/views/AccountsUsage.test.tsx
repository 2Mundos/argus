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
    quota_status: { state: "unknown", reason: "no_usage_records", windows: [] },
    billing_day: null,
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
    ...a,
  }) as Account;

const ACCOUNTS: Account[] = [
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
  account({
    id: "u1",
    name: "cursor-team",
    provider: "cursor",
    quota_status: { state: "unknown", reason: "admin_key_required", windows: [] },
  }),
  account({
    id: "k1",
    name: "claude-limited",
    provider: "anthropic",
    token_limits: { "5h": 100_000 },
    quota_status: {
      state: "known",
      reason: null,
      windows: [
        {
          period: "5h",
          limit: 100_000,
          used: 12_000,
          remaining: 88_000,
          used_percent: 12,
          source: "estimated",
        },
      ],
    },
  }),
  account({
    id: "w1",
    name: "openai-key",
    provider: "openai",
    kind: "api_key",
    quota_status: { state: "unknown", reason: "window_not_configured", windows: [] },
  }),
  account({ id: "m1", name: "unmeasured-only", provider: "openai" }),
  account({ id: "g1", name: "gitea-bot", provider: "gitea", kind: "api_key" }),
  account({
    id: "s1",
    name: "codex-stale",
    provider: "openai",
    quota: [
      {
        window: "300m",
        window_minutes: 300,
        used_percent: 40,
        resets_at: null,
        exhausted_until: null,
        observed_at: "2026-10-01T00:00:00Z",
        source: "polled",
      },
    ],
    quota_status: {
      state: "stale",
      reason: null,
      windows: [],
      observed_at: "2026-10-01T00:00:00Z",
      collector: "codex_rate_limits",
    },
  }),
  account({
    id: "u2",
    name: "claude-sub",
    provider: "anthropic",
    quota_status: {
      state: "unknown",
      reason: "oauth_scope_missing",
      windows: [],
      last_probe: {
        status: "failed",
        code: "oauth_scope_missing",
        reason: "the token lacks the scope",
        at: "2026-10-20T00:00:00Z",
      },
    },
  }),
  account({
    id: "cl1",
    name: "claude-login",
    provider: "anthropic",
    quota: [
      {
        window: "300m",
        window_minutes: 300,
        used_percent: 0,
        resets_at: null,
        exhausted_until: null,
        observed_at: new Date().toISOString(),
        source: "polled",
      },
      {
        window: "weekly_opus",
        window_minutes: null,
        used_percent: 100,
        resets_at: null,
        exhausted_until: null,
        observed_at: new Date().toISOString(),
        source: "polled",
      },
    ],
    quota_status: {
      state: "known",
      reason: null,
      windows: [],
      collector: "claude_oauth_usage",
    },
  }),
  account({
    id: "or1",
    name: "openrouter-key",
    provider: "custom",
    kind: "api_key",
    quota_status: {
      state: "known",
      reason: null,
      windows: [],
      collector: "openrouter_key",
      credit: {
        unit: "usd",
        limit: 20,
        remaining: 0,
        used: 20,
        limit_reset: "monthly",
        observed_at: new Date().toISOString(),
        source: "polled",
        collector: "openrouter_key",
      },
    },
  }),
  account({
    id: "cu2",
    name: "cursor-login",
    provider: "cursor",
    quota_status: {
      state: "unknown",
      reason: "no_usage_window_for_login",
      windows: [],
      last_probe: {
        status: "skipped",
        code: "no_usage_window_for_login",
        reason: "the login answered but states no numeric usage or credit",
        at: "2026-10-20T00:00:00Z",
      },
    },
  }),
  account({
    id: "or2",
    name: "openrouter-uncapped",
    provider: "custom",
    kind: "api_key",
    quota_status: {
      state: "known",
      reason: null,
      windows: [],
      credit: {
        unit: "usd",
        limit: null,
        remaining: null,
        used: 3.25,
        observed_at: new Date().toISOString(),
        source: "polled",
      },
    },
  }),
  account({
    id: "p1",
    name: "codex-failed",
    provider: "openai",
    quota_status: {
      state: "unknown",
      reason: "probe_failed",
      windows: [],
      last_probe: { status: "failed", reason: "TimeoutError", at: "2026-10-20T00:00:00Z" },
    },
  }),
];

const bucket = (start: string, input: number) => ({
  start,
  input_tokens: input,
  output_tokens: 10,
  cached_input_tokens: 5,
  cost_usd: 0,
  runs: 1,
});

const mock = vi.hoisted(() => ({
  queries: [] as Record<string, string>[],
  refreshed: 0,
  outcome: "rate_limited",
}));

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
      const ids = req.query?.account_id ? [req.query.account_id] : ACCOUNTS.map((a) => a.id);
      return {
        bucket: req.query?.bucket ?? "day",
        accounts: ids.map((id) => ({
          account_id: id,
          reported: ["c1", "k1", "m1"].includes(id),
          totals:
            id === "m1"
              ? {
                  ...usage,
                  runs: 4,
                  unmeasured_runs: 4,
                  measured_runs: 0,
                  unmeasured_reasons: { no_usage_in_output: 3, unknown: 1 },
                  runs_by_role: { executor: 4 },
                }
              : {
                  ...usage,
                  input_tokens: 300,
                  output_tokens: 30,
                  cached_input_tokens: 15,
                  runs: id === "k1" ? 5 : 3,
                  unmeasured_runs: id === "k1" ? 2 : 0,
                },
          buckets: [bucket("2026-10-16T00:00:00Z", 100), bucket("2026-10-17T00:00:00Z", 200)],
        })),
      };
    }
    if (req.path === "/api/accounts/{account_id}/quota/refresh") {
      mock.refreshed += 1;
      return { status: mock.outcome, code: null, reason: null, quota: [] };
    }
    return [];
  },
}));

afterEach(() => {
  cleanup();
  mock.queries = [];
  mock.refreshed = 0;
  mock.outcome = "rate_limited";
});

describe("accounts usage", () => {
  it("shows quota left, sparklines, and what is not reported", async () => {
    render(<Accounts />);
    const codex = (await screen.findByText("codex-main")).closest("tr") as HTMLElement;
    expect(within(codex).getByText("63% left")).toBeTruthy();
    expect(within(codex).getByText("19% left")).toBeTruthy();
    expect(within(codex).getByRole("img", { name: /tokens per day/ })).toBeTruthy();
    const cursor = screen.getByText("cursor-team").closest("tr") as HTMLElement;
    expect(within(cursor).getByText(/team Admin API/)).toBeTruthy();
    expect(within(cursor).queryByText(/does not expose/)).toBeNull();
    expect(within(cursor).queryByText("unknown")).toBeNull();
    expect(within(cursor).getByText("not reported")).toBeTruthy();
    const gitea = screen.getByText("gitea-bot").closest("tr") as HTMLElement;
    expect(within(gitea).queryByText("not reported")).toBeNull();
  });

  it("shows estimated windows, a reason when quota is unknown, and unmeasured runs", async () => {
    render(<Accounts />);
    const claude = (await screen.findByText("claude-limited")).closest("tr") as HTMLElement;
    expect(within(claude).getByText("88% left")).toBeTruthy();
    expect(within(claude).getByText("12k / 100k (est.)")).toBeTruthy();
    expect(within(claude).getByText("2 runs without measurement")).toBeTruthy();
    expect(within(claude).getByText("330")).toBeTruthy();
    const key = screen.getByText("openai-key").closest("tr") as HTMLElement;
    expect(within(key).getByText("No quota window configured")).toBeTruthy();
    // Every run lacked figures: no sparkline and no 0 total, just the count.
    const none = screen.getByText("unmeasured-only").closest("tr") as HTMLElement;
    expect(within(none).getByText("No usage reported yet")).toBeTruthy();
    expect(within(none).getByText("4 runs without measurement")).toBeTruthy();
    expect(within(none).queryByRole("img", { name: /tokens per day/ })).toBeNull();
    const label = within(none).getByText("Not measured");
    expect(label.getAttribute("title")).toBe("no usage in output ×3, unknown ×1");
    expect(within(none).getByText("Executor 4")).toBeTruthy();
  });

  it("flags stale snapshots, names why quota is unknown, and shows the last probe", async () => {
    render(<Accounts />);
    const stale = (await screen.findByText("codex-stale")).closest("tr") as HTMLElement;
    expect(within(stale).getByText("60% left")).toBeTruthy();
    expect(within(stale).getByText(/stale, observed/)).toBeTruthy();
    const claude = screen.getByText("claude-sub").closest("tr") as HTMLElement;
    expect(within(claude).getByText(/not setup-token/)).toBeTruthy();
    expect(within(claude).queryByText(/does not expose/)).toBeNull();
    const failed = screen.getByText("codex-failed").closest("tr") as HTMLElement;
    const why = within(failed).getByText("The last quota read failed");
    expect(why.getAttribute("title")).toMatch(/Last read failed \(TimeoutError\)/);
  });

  it("shows Claude windows (zero and full as they are) and provider credit as money", async () => {
    render(<Accounts />);
    const login = (await screen.findByText("claude-login")).closest("tr") as HTMLElement;
    expect(within(login).getByText("100% left")).toBeTruthy(); // 0% used
    expect(within(login).getByText("0% left")).toBeTruthy(); // 100% used
    expect(within(login).getByText(/Weekly · Opus/)).toBeTruthy();
    const capped = screen.getByText("openrouter-key").closest("tr") as HTMLElement;
    expect(within(capped).getByText("$0.00 left of $20.00 · resets monthly")).toBeTruthy();
    const cursorLogin = screen.getByText("cursor-login").closest("tr") as HTMLElement;
    expect(within(cursorLogin).getByText(/states no numeric usage or credit/)).toBeTruthy();
    expect(within(cursorLogin).queryByText(/0% left/)).toBeNull();
    expect(within(cursorLogin).queryByText(/\$0\.00/)).toBeNull();
    const open = screen.getByText("openrouter-uncapped").closest("tr") as HTMLElement;
    expect(within(open).getByText("No spending cap on this key · $3.25 used")).toBeTruthy();
    expect(within(open).queryByText(/\$0\.00/)).toBeNull(); // unknown remaining is not zero
  });

  it("shows the detail of a not-measured account with dashes and reasons", async () => {
    render(<Accounts />);
    fireEvent.click(await screen.findByText("unmeasured-only"));
    const detail = await screen.findByRole("complementary", { name: "Account unmeasured-only" });
    expect(await within(detail).findByText(/no run gave figures/)).toBeTruthy();
    expect(within(detail).getByText(/no usage in output ×3/)).toBeTruthy();
    expect(within(detail).getByText("Runs: Executor 4")).toBeTruthy();
    // Total, input, cached input and output read "—", never 0.
    expect(within(detail).getAllByText("—")).toHaveLength(4);
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

  it("offers a refresh on a Claude account and never reports a skip as an update", async () => {
    mock.outcome = "skipped";
    render(<Accounts />);
    fireEvent.click(await screen.findByText("claude-sub"));
    const detail = await screen.findByRole("complementary", { name: "Account claude-sub" });
    fireEvent.click(within(detail).getByRole("button", { name: "Refresh quota" }));
    expect(await within(detail).findByText(/^Not read:/)).toBeTruthy();
    expect(within(detail).queryByText("Quota updated.")).toBeNull();
  });
});
