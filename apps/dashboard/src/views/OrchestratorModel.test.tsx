// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OrchestratorModelPicker } from "./OrchestratorModel";

const mock = vi.hoisted(() => ({
  put: vi.fn(async () => ({})),
  reset: vi.fn(async () => ({})),
}));

vi.mock("../settings", () => ({ putSetting: mock.put, resetSetting: mock.reset }));

afterEach(cleanup);

beforeEach(() => {
  mock.put.mockClear();
  mock.reset.mockClear();
});

function pick(provider: string, model?: string) {
  fireEvent.change(screen.getByLabelText("orchestrator provider"), {
    target: { value: provider },
  });
  if (model !== undefined) {
    fireEvent.change(screen.getByLabelText("orchestrator model"), { target: { value: model } });
  }
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
}

describe("OrchestratorModelPicker", () => {
  it.each([
    ["local", undefined, "local"],
    ["local", " gemma4 ", "local/gemma4"],
    ["claude", "claude-sonnet-5-5", "claude/claude-sonnet-5-5"],
  ])("writes %s with model %s as %s", async (provider, model, value) => {
    render(<OrchestratorModelPicker value={null} onChanged={() => {}} />);
    pick(provider, model);
    await waitFor(() => expect(mock.put).toHaveBeenCalledWith("orchestrator.model", value, null));
  });

  it("needs a model for a harness engine", () => {
    render(<OrchestratorModelPicker value={null} onChanged={() => {}} />);
    pick("codex");
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("resets the preference for Automatic", async () => {
    render(<OrchestratorModelPicker value="local/qwen3" onChanged={() => {}} />);
    expect((screen.getByLabelText("orchestrator model") as HTMLInputElement).value).toBe("qwen3");
    pick("");
    await waitFor(() => expect(mock.reset).toHaveBeenCalledWith("orchestrator.model", null));
  });
});
