// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api";
import type { ModelPolicy } from "../models";
import { ModelPreferences } from "./ModelPreferences";

const REV = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90";
const NEW_REV = "ffffffff00000000000000000000000000000000000000000000000000000000";

const policy = (over: Partial<ModelPolicy> = {}): ModelPolicy => ({
  harness: { codex: ["gpt-6-luna"], claude: ["sonnet", "opus"] },
  implementation: { codex: ["gpt-6.1-sol"] },
  revision: REV,
  source: "defaults",
  path: "/data/model-policy.json",
  env_overrides: false,
  file_revision: null,
  notice: null,
  ...over,
});

const m = vi.hoisted(() => ({
  fetchModelPolicy: vi.fn(),
  saveModelPolicy: vi.fn(),
  restoreModelDefaults: vi.fn(),
}));

vi.mock("../models", async (orig) => ({ ...(await orig<object>()), ...m }));

vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  return {
    useLiveData: <T,>(load: () => Promise<T>) => {
      const [data, setData] = useState<T>();
      const [error, setError] = useState<Error>();
      useEffect(() => {
        load().then(setData, setError);
      }, []);
      return { data, error, reload: () => {} };
    },
  };
});

beforeEach(() => {
  m.fetchModelPolicy.mockResolvedValue(policy());
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ModelPreferences", () => {
  it("shows chains in order with source and short revision", async () => {
    render(<ModelPreferences />);
    expect(await screen.findByText("sonnet")).toBeTruthy();
    const items = screen.getAllByRole("listitem").map((li) => li.textContent);
    expect(items.filter((t) => t?.includes("sonnet") || t?.includes("opus")).length).toBe(2);
    expect(items.findIndex((t) => t?.includes("sonnet"))).toBeLessThan(
      items.findIndex((t) => t?.includes("opus")),
    );
    expect(screen.getByText("defaults")).toBeTruthy();
    const rev = screen.getByText(REV.slice(0, 8));
    expect(rev.getAttribute("title")).toBe(REV);
    expect(screen.getByText(/Availability at the provider is not verified/)).toBeTruthy();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("edits a chain, saves through the route and shows the new revision", async () => {
    m.saveModelPolicy.mockResolvedValue(
      policy({ source: "file", revision: NEW_REV, file_revision: NEW_REV }),
    );
    render(<ModelPreferences />);
    await screen.findByText("sonnet");
    const save = screen.getByRole("button", { name: "Save" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("new model for harness claude"), {
      target: { value: "haiku" },
    });
    fireEvent.click(screen.getByRole("button", { name: "add model to harness claude" }));
    fireEvent.click(screen.getByRole("button", { name: "move haiku up in harness claude" }));
    fireEvent.click(screen.getByRole("button", { name: "remove sonnet from harness claude" }));
    fireEvent.click(save);
    await waitFor(() => expect(m.saveModelPolicy).toHaveBeenCalledTimes(1));
    expect(m.saveModelPolicy.mock.calls[0]?.[0]).toEqual({
      harness: { codex: ["gpt-6-luna"], claude: ["haiku", "opus"] },
      implementation: { codex: ["gpt-6.1-sol"] },
    });
    expect(await screen.findByText(/Saved: revision ffffffff, source file/)).toBeTruthy();
  });

  it("edits every provider, incl. a Cursor chain and fallbacks, and shows it persisted", async () => {
    const saved = policy({
      source: "file",
      revision: NEW_REV,
      file_revision: NEW_REV,
      harness: { codex: ["luna2"], claude: ["sonnet", "opus"] },
      implementation: {
        codex: ["sol-7"],
        claude: ["opus", "sonnet"],
        agent: ["composer-1", "auto"],
      },
    });
    m.saveModelPolicy.mockResolvedValue(saved);
    render(<ModelPreferences />);
    await screen.findByText("gpt-6.1-sol");
    const addModel = (label: string, id: string) => {
      fireEvent.change(screen.getByLabelText(`new model for ${label}`), { target: { value: id } });
      fireEvent.click(screen.getByRole("button", { name: `add model to ${label}` }));
    };
    addModel("harness codex", "luna2");
    fireEvent.click(screen.getByRole("button", { name: "remove gpt-6-luna from harness codex" }));
    addModel("implementation codex", "sol-7");
    fireEvent.click(
      screen.getByRole("button", { name: "remove gpt-6.1-sol from implementation codex" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "add implementation claude" }));
    addModel("implementation claude", "opus");
    addModel("implementation claude", "sonnet");
    fireEvent.click(screen.getByRole("button", { name: "add implementation agent" }));
    addModel("implementation agent", "composer-1");
    addModel("implementation agent", "auto");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(m.saveModelPolicy).toHaveBeenCalledTimes(1));
    expect(m.saveModelPolicy.mock.calls[0]?.[0]).toEqual({
      harness: saved.harness,
      implementation: saved.implementation,
    });
    // A fresh load (as after navigating away and back) reads the saved policy.
    cleanup();
    m.fetchModelPolicy.mockResolvedValue(saved);
    render(<ModelPreferences />);
    expect(await screen.findByText("composer-1")).toBeTruthy();
    expect(screen.getAllByText("agent (Cursor)").length).toBeGreaterThan(0);
    expect(screen.getByText("sol-7")).toBeTruthy();
    const impl = screen.getAllByRole("listitem").map((li) => li.textContent ?? "");
    const at = (model: string) => impl.findIndex((t) => t.includes(model));
    expect(at("composer-1")).toBeLessThan(at("auto"));
  });

  it("rejects a duplicate or malformed model before calling the API", async () => {
    render(<ModelPreferences />);
    await screen.findByText("sonnet");
    const input = screen.getByLabelText("new model for harness claude");
    fireEvent.change(input, { target: { value: "opus" } });
    expect(screen.getByText(/already in this chain/)).toBeTruthy();
    fireEvent.change(input, { target: { value: "--bad" } });
    expect(screen.getByText(/Model IDs use letters/)).toBeTruthy();
    const add = screen.getByRole("button", { name: "add model to harness claude" });
    expect((add as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows the API's validation error inline and keeps the draft", async () => {
    m.saveModelPolicy.mockRejectedValue(
      new ApiError(422, "validation_failed", "invalid model chain: implementation.codex"),
    );
    render(<ModelPreferences />);
    await screen.findByText("sonnet");
    fireEvent.click(screen.getByRole("button", { name: "move opus up in harness claude" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("invalid model chain: implementation.codex");
    expect(screen.getByRole("button", { name: "Discard changes" })).toBeTruthy();
  });

  it("restores defaults only when a file exists", async () => {
    render(<ModelPreferences />);
    await screen.findByText("sonnet");
    expect(
      (screen.getByRole("button", { name: "Restore defaults" }) as HTMLButtonElement).disabled,
    ).toBe(true);
    cleanup();
    m.fetchModelPolicy.mockResolvedValue(
      policy({ source: "file", file_revision: REV, harness: { codex: ["x"] } }),
    );
    m.restoreModelDefaults.mockResolvedValue(policy());
    render(<ModelPreferences />);
    await screen.findByText("x");
    fireEvent.click(screen.getByRole("button", { name: "Restore defaults" }));
    expect(
      await screen.findByText(/Restored defaults: revision a1b2c3d4, source defaults/),
    ).toBeTruthy();
  });

  it("says the env policy wins over the file", async () => {
    m.fetchModelPolicy.mockResolvedValue(
      policy({ source: "env", env_overrides: true, file_revision: NEW_REV }),
    );
    m.saveModelPolicy.mockResolvedValue(
      policy({ source: "env", env_overrides: true, file_revision: NEW_REV }),
    );
    render(<ModelPreferences />);
    const warning = await screen.findByRole("alert");
    expect(warning.textContent).toContain("ARGUS_MODEL_POLICY");
    expect(warning.textContent).toContain("wins over the file");
    expect(warning.textContent).toContain("no effect");
    fireEvent.click(screen.getByRole("button", { name: "move opus up in harness claude" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText(/still wins, so nothing changed in effect/)).toBeTruthy();
  });

  it("reports a load failure", async () => {
    m.fetchModelPolicy.mockRejectedValue(new ApiError(401, "invalid_token", "bad token"));
    render(<ModelPreferences />);
    expect(await screen.findByText("bad token")).toBeTruthy();
  });
});
