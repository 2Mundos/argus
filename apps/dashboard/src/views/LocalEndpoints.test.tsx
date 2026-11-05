// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Account, LocalAccountView, LocalTestResult } from "../api";
import { Accounts } from "./Accounts";
import { LocalEndpoints } from "./LocalEndpoints";

const usage = { input_tokens: 0, output_tokens: 0, cost_usd: 0 };
const notRun = { status: "not_run" as const };

const view = (over: Partial<LocalAccountView> = {}): LocalAccountView => ({
  base_url: "http://host.docker.internal:8080",
  api_base_url: null,
  api_endpoint: "http://127.0.0.1:8080",
  api_endpoint_source: "derived",
  model: "qwen-stale",
  context_tokens: null,
  orchestrator_model: "spark-x2.6",
  orchestrator_model_source: "orchestrator.model",
  served_models: ["spark-x2.6"],
  discovery: { status: "passed", latency_ms: 3 },
  inference: notRun,
  orchestrator: notRun,
  warnings: ["Stale model: the account's model `qwen-stale` (what workers run) is not served."],
  ...over,
});

const account = (over: Partial<Account> = {}): Account => ({
  id: "loc-1",
  name: "spark",
  provider: "local",
  kind: "api_key",
  secret_ref: "sops:account-loc-1",
  priority: 0,
  max_leases: 1,
  state: "available",
  exhausted_until: null,
  disabled_reason: null,
  labels: {},
  active_leases: 0,
  usage_24h: usage,
  usage_total: usage,
  quota_status: { state: "unknown", reason: "local_no_quota", windows: [] },
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  local: view(),
  ...over,
});

const result = (over: Partial<LocalTestResult> = {}): LocalTestResult => ({
  account_id: "loc-1",
  account_name: "spark",
  runtime: "api",
  endpoint: "http://127.0.0.1:8080",
  endpoint_source: "derived",
  worker_endpoint: "http://host.docker.internal:8080",
  worker_note:
    "Workers reach `http://host.docker.internal:8080` from their containers; the Argus API process cannot test that address from its own network.",
  served_models: ["spark-x2.6"],
  discovery: { status: "passed", latency_ms: 2 },
  inference: notRun,
  orchestrator: notRun,
  warnings: [],
  ...over,
});

const mock = vi.hoisted(() => ({
  puts: [] as Record<string, unknown>[],
  tests: [] as Record<string, unknown>[],
  testTargets: [] as string[],
  saved: undefined as unknown,
  tested: undefined as unknown,
  fail: undefined as Error | undefined,
  list: [] as unknown[],
  listCalls: 0,
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
    GET: (path: string) => (path === "/api/accounts/usage" ? { op: "usage" } : { op: "list" }),
    PUT: (path: string, init: { body: Record<string, unknown> }) => ({ op: "put", path, ...init }),
    POST: (path: string, init: { body: Record<string, unknown> }) => ({
      op: "post",
      path,
      ...init,
    }),
    PATCH: () => ({ op: "patch" }),
  },
  call: async (req: {
    op: string;
    path?: string;
    body?: Record<string, unknown>;
    params?: { path?: { account_id?: string } };
  }) => {
    if (req.op === "usage") return { accounts: [] };
    if (req.op === "list") {
      mock.listCalls += 1;
      return mock.list;
    }
    if (mock.fail) throw mock.fail;
    if (req.op === "put") {
      mock.puts.push(req.body ?? {});
      return mock.saved;
    }
    if (req.path === "/api/accounts/{account_id}/local/test") {
      mock.testTargets.push(req.params?.path?.account_id ?? "");
      mock.tests.push(req.body ?? {});
      return mock.tested;
    }
    return {};
  },
}));

beforeEach(() => {
  mock.puts = [];
  mock.tests = [];
  mock.testTargets = [];
  mock.saved = account();
  mock.tested = result();
  mock.fail = undefined;
  mock.list = [account()];
  mock.listCalls = 0;
});
afterEach(cleanup);

function status(label: string): string | null {
  const checks = screen.getByLabelText("Checks");
  const term = within(checks).getByText(label);
  return term.parentElement?.querySelector("[data-status]")?.getAttribute("data-status") ?? null;
}

describe("LocalEndpoints", () => {
  it("shows both endpoints, the API endpoint's source and the stale model warning", () => {
    render(<LocalEndpoints account={account()} onClose={() => {}} onSaved={() => {}} />);
    expect(screen.getByDisplayValue("http://host.docker.internal:8080")).toBeTruthy();
    expect(screen.getByTestId("api-endpoint").textContent).toContain(
      "http://127.0.0.1:8080 (derived",
    );
    expect(screen.getByText(/runs/).textContent).toContain("from orchestrator.model");
    expect(within(screen.getByLabelText("Warnings")).getByText(/Stale model/)).toBeTruthy();
    // Nothing has been tested: the completions are not run, not passed.
    expect(status("Inference")).toBe("not_run");
    expect(status("Tool calling (orchestrator)")).toBe("not_run");
  });

  it("blocks tests on an unsaved draft, then tests the saved account after Save", async () => {
    mock.saved = account({
      local: view({
        api_base_url: "http://localhost:9000",
        api_endpoint: "http://localhost:9000",
        api_endpoint_source: "api_base_url",
        model: "spark-x2.6",
        warnings: [],
      }),
    });
    render(<LocalEndpoints account={account()} onClose={() => {}} onSaved={() => {}} />);
    fireEvent.change(screen.getByLabelText(/API endpoint/), {
      target: { value: "http://localhost:9000" },
    });
    fireEvent.change(screen.getByLabelText(/^Model/), { target: { value: "spark-x2.6" } });

    expect(screen.getByText(/Save your endpoint or model changes/)).toBeTruthy();
    for (const name of ["Test connection", "Test inference", "Test tool calling"]) {
      const button = screen.getByRole("button", { name });
      expect(button).toHaveProperty("disabled", true);
      fireEvent.click(button);
    }
    expect(mock.tests).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(mock.puts).toEqual([{ api_base_url: "http://localhost:9000", model: "spark-x2.6" }]),
    );
    expect(screen.getByTestId("api-endpoint").textContent).toContain(
      "http://localhost:9000 (set here)",
    );
    expect(mock.tests).toEqual([]); // Save never runs an inference test.
    const inference = screen.getByRole("button", { name: "Test inference" });
    expect(inference).toHaveProperty("disabled", false);
    fireEvent.click(inference);
    await waitFor(() => expect(mock.tests).toEqual([{ inference: true, tool_calling: false }]));
    expect(mock.testTargets).toEqual(["loc-1"]);
  });

  it("saves only the changed fields and shows the account as saved", async () => {
    mock.saved = account({
      local: view({
        api_base_url: "http://localhost:9000",
        api_endpoint: "http://localhost:9000",
        api_endpoint_source: "api_base_url",
        model: "spark-x2.6",
        warnings: [],
      }),
    });
    const onSaved = vi.fn();
    render(<LocalEndpoints account={account()} onClose={() => {}} onSaved={onSaved} />);
    fireEvent.change(screen.getByLabelText(/API endpoint/), {
      target: { value: "http://localhost:9000" },
    });
    fireEvent.change(screen.getByLabelText(/^Model/), { target: { value: "spark-x2.6" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(mock.puts).toEqual([{ api_base_url: "http://localhost:9000", model: "spark-x2.6" }]);
    expect(screen.getByTestId("api-endpoint").textContent).toContain(
      "http://localhost:9000 (set here)",
    );
    expect(screen.queryByLabelText("Warnings")).toBeNull();
    expect(screen.getByRole("status").textContent).toContain("Saved the endpoints");
  });

  it("shows a refused save and keeps what was typed", async () => {
    mock.fail = new Error("api_base_url host must be one of 127.0.0.1, localhost, ::1");
    const onSaved = vi.fn();
    render(<LocalEndpoints account={account()} onClose={() => {}} onSaved={onSaved} />);
    fireEvent.change(screen.getByLabelText(/API endpoint/), {
      target: { value: "http://172.17.0.1:8080" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/api_base_url host must be/)).toBeTruthy();
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByDisplayValue("http://172.17.0.1:8080")).toBeTruthy();
    expect(screen.getByText(/Save your endpoint or model changes/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Test connection" })).toHaveProperty(
      "disabled",
      true,
    );
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    expect(mock.tests).toEqual([]);
  });

  it("cancels a draft by restoring the saved endpoint and model", () => {
    render(<LocalEndpoints account={account()} onClose={() => {}} onSaved={() => {}} />);
    fireEvent.change(screen.getByLabelText(/API endpoint/), {
      target: { value: "http://localhost:9000" },
    });
    fireEvent.change(screen.getByLabelText(/^Model/), { target: { value: "spark-x2.6" } });
    expect(screen.getByRole("button", { name: "Test tool calling" })).toHaveProperty(
      "disabled",
      true,
    );

    fireEvent.click(screen.getByRole("button", { name: "Cancel changes" }));
    expect(screen.getByLabelText(/API endpoint/)).toHaveProperty("value", "");
    expect(screen.getByLabelText(/^Model/)).toHaveProperty("value", "qwen-stale");
    expect(screen.queryByText(/Save your endpoint or model changes/)).toBeNull();
    expect(screen.getByRole("button", { name: "Test tool calling" })).toHaveProperty(
      "disabled",
      false,
    );
    expect(mock.puts).toEqual([]);
  });

  it("resets the API endpoint, model and window in one call", async () => {
    mock.saved = account({
      local: view({ model: null, orchestrator_model_source: "served", warnings: [] }),
    });
    render(<LocalEndpoints account={account()} onClose={() => {}} onSaved={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Reset to defaults" }));
    await waitFor(() => expect(mock.puts).toHaveLength(1));
    expect(mock.puts[0]).toEqual({ api_base_url: null, model: null, context_tokens: null });
    await waitFor(() => expect(screen.getByLabelText(/^Model/)).toHaveProperty("value", ""));
    expect(screen.getByRole("status").textContent).toContain("Reset");
  });

  it("runs each test only when asked and keeps the statuses apart", async () => {
    render(<LocalEndpoints account={account()} onClose={() => {}} onSaved={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    await waitFor(() => expect(mock.tests).toHaveLength(1));
    expect(mock.tests[0]).toEqual({ inference: false, tool_calling: false });
    // A passing discovery never shows inference or tool calling as passed.
    await waitFor(() => expect(screen.getByText(/cannot test that address/)).toBeTruthy());
    expect(status("Discovery")).toBe("passed");
    expect(status("Inference")).toBe("not_run");

    mock.tested = result({
      orchestrator: {
        status: "failed",
        failure: "no_tool_call",
        model: "spark-x2.6",
        detail: "`spark-x2.6` answered without calling the offered tool.",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Test tool calling" }));
    await waitFor(() => expect(mock.tests).toHaveLength(2));
    expect(mock.tests[1]).toEqual({ inference: false, tool_calling: true });
    await waitFor(() => expect(status("Tool calling (orchestrator)")).toBe("failed"));
    expect(screen.getByText(/without calling the offered tool/)).toBeTruthy();

    mock.tested = result({
      inference: { status: "skipped", failure: "model_not_served", model: "qwen-stale" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Test inference" }));
    await waitFor(() => expect(mock.tests).toHaveLength(3));
    expect(mock.tests[2]).toEqual({ inference: true, tool_calling: false });
    await waitFor(() => expect(status("Inference")).toBe("skipped"));
  });

  it("shows a failed discovery's sentence and a failed request", async () => {
    mock.tested = result({
      discovery: {
        status: "failed",
        failure: "dns_failed",
        detail: "`host.docker.internal` does not resolve from the Argus API process.",
      },
      inference: {
        status: "skipped",
        failure: "dns_failed",
        detail: "Not sent: discovery failed.",
      },
    });
    render(<LocalEndpoints account={account()} onClose={() => {}} onSaved={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Test inference" }));
    await waitFor(() => expect(status("Discovery")).toBe("failed"));
    expect(screen.getByText(/does not resolve from the Argus API process/)).toBeTruthy();
    expect(status("Inference")).toBe("skipped");

    mock.fail = new Error("forbidden: requires admin");
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    expect((await screen.findByRole("alert")).textContent).toContain("requires admin");
  });

  it("asks for a credential when the stored endpoint is unreadable", () => {
    render(
      <LocalEndpoints account={account({ local: null })} onClose={() => {}} onSaved={() => {}} />,
    );
    expect(screen.getByRole("alert").textContent).toContain("Replace the account's credential");
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });
});

describe("Accounts with a local account", () => {
  it("flags a stale model and opens the endpoints from the row, then reloads", async () => {
    render(<Accounts />);
    expect(await screen.findByText("stale model")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Local endpoints of spark" }));
    const dialog = await screen.findByRole("form", { name: "Local endpoints of spark" });
    fireEvent.change(within(dialog).getByLabelText(/^Model/), { target: { value: "spark-x2.6" } });
    const before = mock.listCalls;
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mock.puts).toEqual([{ model: "spark-x2.6" }]));
    await waitFor(() => expect(mock.listCalls).toBeGreaterThan(before));
  });

  it("offers no endpoints action on a cloud account", async () => {
    mock.list = [account({ id: "c-1", name: "claude", provider: "anthropic", local: null })];
    render(<Accounts />);
    expect(await screen.findByRole("button", { name: "Edit claude" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Local endpoints of claude" })).toBeNull();
  });
});
