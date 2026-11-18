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
    {
      id: "nanoclaw:harness:claude:anthropic",
      role: "harness",
      adapter: "claude",
      provider: "anthropic",
      model_sources: ["explicit"],
      modes: ["single", "paired"],
      efforts: ["low", "medium", "high", "xhigh", "max"],
      effort_interface: "NanoClaw container config effort -> Agent SDK query({effort})",
      effort_model_rules: [{ models: ["claude-opus-4-5"], efforts: ["low", "medium", "high"] }],
      effort_unlisted_models: "supported",
    },
    {
      id: "nanoclaw:executor:codex:openai",
      role: "executor",
      adapter: "codex",
      provider: "openai",
      model_sources: ["explicit"],
      modes: ["paired"],
      efforts: ["low", "medium", "high", "xhigh", "max", "ultra"],
      effort_interface: 'codex exec -c model_reasoning_effort="<level>"',
      effort_model_rules: [],
      effort_unlisted_models: "unverified",
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
  it("edits review models under their own key and copy", async () => {
    render(
      <ExecutionChains
        project={null}
        setting={undefined}
        settingKey="review.chains"
        title="Review models"
        unconfigured="No review models: the reviewer is the first non-author model in the model preferences."
        onChanged={vi.fn()}
      />,
    );
    expect(screen.getAllByText(/No review models/)).toHaveLength(4);
    fireEvent.click(screen.getByRole("button", { name: "Add hard alternative" }));
    fireEvent.click(screen.getByRole("button", { name: "Save review models" }));
    await waitFor(() =>
      expect(saved.put).toHaveBeenCalledWith(
        "review.chains",
        { version: 1, difficulties: { hard: [expect.objectContaining({ mode: "single" })] } },
        null,
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Reset review models" }));
    await waitFor(() => expect(saved.reset).toHaveBeenCalledWith("review.chains", null));
  });
  it("sets effort per role from the supported levels and blocks a level the model lacks", async () => {
    render(<ExecutionChains {...base} setting={setting} />);
    // Inherited alternatives show their effort; a local claude harness has none to set.
    expect(screen.getByText(/local \/ local · effort runtime default/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Override hard" }));
    const localEffort = screen.getByLabelText("harness reasoning effort") as HTMLSelectElement;
    expect(localEffort.disabled).toBe(true);
    expect(
      screen.getByText(
        "claude / local has no reasoning effort setting for the harness; the runtime default applies.",
      ),
    ).toBeTruthy();
    fireEvent.change(screen.getByLabelText("hard alternative 1 mode"), {
      target: { value: "paired" },
    });
    fireEvent.change(screen.getByLabelText("harness adapter / provider"), {
      target: { value: "nanoclaw:harness:claude:anthropic" },
    });
    fireEvent.change(screen.getByLabelText("harness model ID"), {
      target: { value: "claude-opus-5-5" },
    });
    const harnessEffort = screen.getByLabelText("harness reasoning effort") as HTMLSelectElement;
    expect([...harnessEffort.options].map((o) => o.value)).toEqual([
      "",
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
    fireEvent.change(harnessEffort, { target: { value: "max" } });
    fireEvent.change(screen.getByLabelText("executor adapter / provider"), {
      target: { value: "nanoclaw:executor:codex:openai" },
    });
    fireEvent.change(screen.getByLabelText("executor model ID"), {
      target: { value: "gpt-6.1-sol" },
    });
    expect(screen.getByText(/not in the verified list/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("executor reasoning effort"), {
      target: { value: "ultra" },
    });
    // Opus 4.5 takes no max: Save names the problem until the level changes.
    fireEvent.change(screen.getByLabelText("harness model ID"), {
      target: { value: "claude-opus-4-5-20251101" },
    });
    const save = screen.getByRole("button", { name: "Save execution chains" }) as HTMLButtonElement;
    expect(screen.getByRole("status").textContent).toBe(
      "hard, alternative 1: harness effort: max is not supported by this claude model",
    );
    expect(save.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("harness reasoning effort"), {
      target: { value: "high" },
    });
    fireEvent.click(save);
    await waitFor(() => expect(saved.put).toHaveBeenCalled());
    const [, value] = saved.put.mock.calls[0] as unknown as [
      string,
      { difficulties: { hard: { harness: object; executor: object }[] } },
    ];
    expect(value.difficulties.hard[0]?.harness).toMatchObject({ effort: "high" });
    expect(value.difficulties.hard[0]?.executor).toMatchObject({ effort: "ultra" });
    // Runtime default removes the key so the saved alternative is the pre-effort shape.
    fireEvent.change(screen.getByLabelText("executor reasoning effort"), { target: { value: "" } });
    fireEvent.click(save);
    await waitFor(() => expect(saved.put).toHaveBeenCalledTimes(2));
    const [, again] = saved.put.mock.calls[1] as unknown as [
      string,
      { difficulties: { hard: { executor: object }[] } },
    ];
    expect(again.difficulties.hard[0]?.executor).not.toHaveProperty("effort");
  });
  it("shows each level's built-in executor order where no chain exists", () => {
    render(<ExecutionChains {...base} setting={undefined} />);
    expect(screen.getAllByText(/executor order agent → claude → codex/)).toHaveLength(2);
    expect(screen.getAllByText(/executor order claude → codex/)).toHaveLength(2);
    expect(screen.getByText("claude / local: harness or executor")).toBeTruthy();
    expect(screen.getByText("opencode / custom: executor only")).toBeTruthy();
  });
});
