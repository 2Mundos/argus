// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Engines } from "./Engines";

const sample = {
  engines: [
    {
      id: "codex",
      label: "Codex (OpenAI)",
      role: "code_executor",
      axes: {
        model_provider: "openai",
        model: null,
        harness: "nanoclaw provider codex",
        executor: "codex",
        account_provider: "openai",
      },
      auth_methods: ["chatgpt-device-login"],
      version: "0.162.1",
      readiness: {
        installed: { ok: true, detail: "ok" },
        authenticated: { ok: false, detail: "no credential" },
        validated: { ok: null, detail: "not checked" },
      },
      usable: false,
      layers: [
        {
          layer: "harness",
          readiness: {
            installed: { ok: true, detail: "ok" },
            authenticated: { ok: false, detail: "no credential" },
            validated: { ok: null, detail: "not checked" },
          },
          usable: false,
        },
        {
          layer: "executor",
          readiness: {
            installed: { ok: true, detail: "ok" },
            authenticated: { ok: true, detail: "ok" },
            validated: { ok: true, detail: "ok" },
          },
          usable: true,
        },
      ],
      notes: "",
    },
  ],
  local: { reachable: true, model: "qwen", n_ctx: 65536, slots: 1, error: null },
};

vi.mock("../engines", () => ({
  fetchEngineCatalog: vi.fn(async () => sample),
  readinessLabel: (f: { ok: boolean | null }) =>
    f.ok === true ? "yes" : f.ok === false ? "no" : "unknown",
}));

vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  return {
    useLiveData: <T,>(load: () => Promise<T>) => {
      const [data, setData] = useState<T>();
      useEffect(() => {
        load().then(setData);
      }, []);
      return { data, error: undefined, reload: () => {} };
    },
  };
});

afterEach(cleanup);

describe("Engines", () => {
  it("shows provider, executor and separate readiness facts", async () => {
    render(<Engines />);
    expect(await screen.findByText("Codex (OpenAI)")).toBeTruthy();
    expect(screen.getAllByText("openai")).toHaveLength(2);
    expect(screen.getAllByText("codex").length).toBeGreaterThan(0);
    expect(screen.getByRole("link", { name: "Settings" }).getAttribute("href")).toBe(
      "#setup/settings",
    );
    expect(screen.getAllByText("no").length).toBeGreaterThan(0);
    // Harness and executor are separate rows with their own readiness.
    expect(screen.getByText("harness", { selector: "td" })).toBeTruthy();
    expect(screen.getByText("executor", { selector: "td" })).toBeTruthy();
  });
});
