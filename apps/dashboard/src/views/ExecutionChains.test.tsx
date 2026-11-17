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
vi.mock("../live", () => ({
  useLiveData: (load: unknown) => ({
    data: String(load).includes("engines")
      ? {
          execution_capabilities: [
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
          ],
        }
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

beforeEach(() => vi.clearAllMocks());
afterEach(cleanup);
describe("execution chain editor", () => {
  it("preserves per-level inheritance without materializing it on project save", async () => {
    render(<ExecutionChains project="demo" setting={setting} onChanged={vi.fn()} />);
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
    render(<ExecutionChains project="demo" setting={setting} onChanged={vi.fn()} />);
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
    const view = render(<ExecutionChains project="demo" setting={setting} onChanged={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Override hard" }));
    fireEvent.click(screen.getByRole("button", { name: "Inherit / reset hard" }));
    expect(screen.getByRole("button", { name: "Override hard" })).toBeTruthy();
    view.rerender(
      <ExecutionChains
        project="demo"
        setting={{ ...setting, notice: "legacy_env_lock" }}
        onChanged={vi.fn()}
      />,
    );
    expect(
      (screen.getByRole("button", { name: "Save execution chains" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Reset execution chains" }));
    await waitFor(() => expect(saved.reset).toHaveBeenCalledWith("execution.chains", "demo"));
  });
});
