// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Accounts } from "./Accounts";

const usage = { input_tokens: 0, output_tokens: 0, cost_usd: 0 };
const account = (id: string, name: string, provider: string) => ({
  id,
  name,
  provider,
  kind: "subscription",
  secret_ref: `sops:${id}`,
  priority: 3,
  max_leases: null,
  state: "disabled",
  exhausted_until: null,
  disabled_reason: "disabled by admin",
  labels: {},
  active_leases: 0,
  usage_24h: usage,
  usage_total: usage,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
});

vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  return {
    useLiveData: <T,>(load: () => Promise<T>) => {
      const [data, setData] = useState<T>();
      useEffect(() => {
        load().then(setData);
      }, []);
      return { data, error: undefined, reload: () => load().then(setData) };
    },
  };
});

vi.mock("../api", () => ({
  api: { GET: (path: string) => path, POST: () => "post", PUT: () => "put", PATCH: () => "patch" },
  call: async (path: string) =>
    path === "/api/accounts"
      ? [account("a1", "laude", "anthropic"), account("o1", "codex", "openai")]
      : path === "/api/accounts/usage"
        ? { accounts: [] }
        : [],
}));

afterEach(cleanup);

describe("Accounts: Claude wizard entry points", () => {
  it("adds a Claude account from the toolbar", async () => {
    render(<Accounts />);
    await screen.findByText("laude");
    fireEvent.click(screen.getByRole("button", { name: "Add Claude account" }));
    expect(screen.getByRole("dialog").getAttribute("aria-label")).toBe("Add Claude account");
    expect(screen.getByLabelText("Name")).toBeTruthy();
  });

  it("renews an existing Anthropic account, prefilled, and only those", async () => {
    render(<Accounts />);
    await screen.findByText("laude");
    expect(screen.getAllByRole("button", { name: /^Renew credential/ })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Renew credential of laude" }));
    expect(screen.getByRole("dialog").getAttribute("aria-label")).toBe("Renew Claude account");
    expect(screen.getByText(/“laude”/)).toBeTruthy();
    expect(screen.queryByLabelText("Name")).toBeNull();
  });
});
