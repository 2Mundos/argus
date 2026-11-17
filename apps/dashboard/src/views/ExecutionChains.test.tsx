// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SettingValue } from "../settings";
import { ExecutionChains } from "./ExecutionChains";

const saved = vi.hoisted(() => ({ put: vi.fn(async () => ({})), reset: vi.fn(async () => ({})) }));
vi.mock("../settings", async (original) => ({
  ...(await original<object>()),
  putSetting: saved.put,
  resetSetting: saved.reset,
}));
const capabilities = vi.hoisted(() => {
  const all = [
    {
      id: "nanoclaw:harness:claude:local",
      role: "harness",
      adapter: "claude",
      provider: "local",
      model_sources: ["account"],
      modes: ["single", "paired"],
    },
    {
      id: "nanoclaw:executor:opencode:custom",
      role: "executor",
      adapter: "opencode",
      provider: "custom",
      model_sources: ["account", "explicit"],
      modes: ["paired"],
    },
  ];
  return { all, list: all };
});
vi.mock("../live", () => ({
  useLiveData: (load: unknown) => ({
    data: String(load).includes("engines")
      ? { execution_capabilities: capabilities.list }
      : [
          { id: "local", name: "Local fixture", provider: "local", state: "available" },
          {
            id: "custom",
            name: "Custom fixture",
            provider: "custom",
            state: "available",
            config: { executor: { model: "fixture/code" } },
          },
        ],
  }),
}));
const direct = {
  id: "direct",
  mode: "single",
  runtime: "nanoclaw",
  harness: {
    adapter: "claude",
    provider: "local",
    account_id: "local",
    model: { source: "account" },
  },
};
const setting: SettingValue = {
  key: "execution.chains",
  source: "global",
  value: { version: 1, difficulties: { hard: [direct] } },
  scoped_value: { version: 1, difficulties: {} },
  difficulty_sources: { hard: "global" },
};

const base = {
  project: "demo",
  legacyExecutors: {
    trivial: ["agent", "claude", "codex"],
    simple: ["agent", "claude", "codex"],
    standard: ["claude", "codex"],
    hard: ["claude", "codex"],
  },
  onChanged: vi.fn(),
};

beforeEach(() => {
  vi.clearAllMocks();
  capabilities.list = capabilities.all;
});
afterEach(cleanup);
describe("execution chain editor", () => {
  it("preserves per-level inheritance without materializing it on project save", async () => {
    render(<ExecutionChains {...base} setting={setting} />);
    expect(
      screen.getByText("Source: global · revision — · inherited (read only until overridden)"),
    ).toBeTruthy();
    for (const d of ["trivial", "simple", "standard", "hard"])
      expect(screen.getByRole("button", { name: `Add ${d} alternative` })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save execution chains" }));
    await waitFor(() =>
      expect(saved.put).toHaveBeenCalledWith(
        "execution.chains",
        { version: 1, difficulties: {} },
        "demo",
      ),
    );
  });
  it("saves exact mixed alternatives and reorders the entire pair", async () => {
    render(<ExecutionChains {...base} setting={setting} />);
    fireEvent.click(screen.getByRole("button", { name: "Override hard" }));
    fireEvent.click(screen.getByRole("button", { name: "Add hard alternative" }));
    fireEvent.change(screen.getByLabelText("hard alternative 2 mode"), {
      target: { value: "paired" },
    });
    const harnesses = screen.getAllByLabelText("harness account");
    fireEvent.change(harnesses[1]!, { target: { value: "local" } });
    fireEvent.change(screen.getByLabelText("executor adapter / provider"), {
      target: { value: "nanoclaw:executor:opencode:custom" },
    });
    fireEvent.change(screen.getByLabelText("executor account"), { target: { value: "custom" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Move up" })[1]!);
    fireEvent.click(screen.getByRole("button", { name: "Save execution chains" }));
    await waitFor(() => expect(saved.put).toHaveBeenCalled());
    const value = saved.put.mock.calls[0] as unknown as [
      string,
      { difficulties: { hard: { mode: string; executor?: { account_id: string } }[] } },
    ];
    expect(value[1].difficulties.hard.map((a) => a.mode)).toEqual(["paired", "single"]);
    expect(value[1].difficulties.hard[0]?.executor?.account_id).toBe("custom");
    expect(value[1].difficulties.hard[1]).not.toHaveProperty("executor");
  });
  it("per-level reset restores inheritance and env locks prevent saving", async () => {
    const view = render(<ExecutionChains {...base} setting={setting} />);
    fireEvent.click(screen.getByRole("button", { name: "Override hard" }));
    fireEvent.click(screen.getByRole("button", { name: "Inherit / reset hard" }));
    expect(screen.getByRole("button", { name: "Override hard" })).toBeTruthy();
    view.rerender(
      <ExecutionChains {...base} setting={{ ...setting, notice: "legacy_env_lock" }} />,
    );
    expect(
      (screen.getByRole("button", { name: "Save execution chains" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Reset execution chains" }));
    await waitFor(() => expect(saved.reset).toHaveBeenCalledWith("execution.chains", "demo"));
  });
  it("saves a single-layer local route with no account chosen", async () => {
    render(<ExecutionChains {...base} setting={undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "Add trivial alternative" }));
    expect((screen.getByLabelText("harness account") as HTMLSelectElement).value).toBe("");
    const save = screen.getByRole("button", { name: "Save execution chains" });
    expect((save as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(saved.put).toHaveBeenCalled());
    const [, value] = saved.put.mock.calls[0] as unknown as [string, { difficulties: object }];
    expect(value.difficulties).toEqual({
      trivial: [
        {
          id: "alternative-1",
          mode: "single",
          runtime: "nanoclaw",
          harness: { adapter: "claude", provider: "local", model: { source: "account" } },
        },
      ],
    });
    // Pinning is optional and unpinning removes the key again.
    fireEvent.change(screen.getByLabelText("harness account"), { target: { value: "local" } });
    fireEvent.change(screen.getByLabelText("harness account"), { target: { value: "" } });
    expect(screen.getByRole("button", { name: "Save execution chains" })).toBeTruthy();
  });
  it("names an empty explicit model ID, blocks Save until it is fixed", async () => {
    render(<ExecutionChains {...base} setting={setting} />);
    fireEvent.click(screen.getByRole("button", { name: "Override hard" }));
    fireEvent.click(screen.getByRole("button", { name: "Add hard alternative" }));
    fireEvent.change(screen.getByLabelText("hard alternative 2 mode"), {
      target: { value: "paired" },
    });
    fireEvent.change(screen.getByLabelText("executor adapter / provider"), {
      target: { value: "nanoclaw:executor:opencode:custom" },
    });
    fireEvent.change(screen.getByLabelText("executor model source"), {
      target: { value: "explicit" },
    });
    const save = screen.getByRole("button", { name: "Save execution chains" }) as HTMLButtonElement;
    const list = screen.getByRole("status");
    expect(list.textContent).toBe("hard, alternative 2: executor model ID is empty");
    expect(save.disabled).toBe(true);
    expect(save.getAttribute("aria-describedby")).toBe(list.id);
    fireEvent.change(screen.getByLabelText("executor model ID"), {
      target: { value: "fixture/code" },
    });
    expect(screen.queryByRole("status")).toBeNull();
    expect(save.disabled).toBe(false);
    expect(save.getAttribute("aria-describedby")).toBeNull();
  });
  it("reports an invalid alternative ID", () => {
    render(<ExecutionChains {...base} setting={setting} />);
    fireEvent.click(screen.getByRole("button", { name: "Override hard" }));
    fireEvent.change(screen.getByLabelText("hard alternative 1 ID"), {
      target: { value: "bad id!" },
    });
    expect(screen.getByRole("status").textContent).toBe(
      "hard, alternative 1: alternative ID must match [A-Za-z0-9_-]{1,64}",
    );
    expect(
      (screen.getByRole("button", { name: "Save execution chains" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });
  it("shows the capability line when no capabilities are loaded", () => {
    capabilities.list = [];
    render(<ExecutionChains {...base} setting={setting} />);
    expect(screen.getByRole("status").textContent).toBe(
      "No execution capabilities loaded from /api/engines",
    );
    expect(
      (screen.getByRole("button", { name: "Save execution chains" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });
  it("shows each level's built-in executor order where no chain exists", () => {
    render(<ExecutionChains {...base} setting={undefined} />);
    expect(screen.getAllByText(/executor order agent → claude → codex/)).toHaveLength(2);
    expect(screen.getAllByText(/executor order claude → codex/)).toHaveLength(2);
    expect(screen.getByText("claude / local: harness or executor")).toBeTruthy();
    expect(screen.getByText("opencode / custom: executor only")).toBeTruthy();
  });
});
