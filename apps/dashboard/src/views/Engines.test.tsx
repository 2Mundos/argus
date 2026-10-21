// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Engines } from "./Engines";

const sample = {
  engines: [
    {
      id: "codex",
      label: "Codex (OpenAI)",
      role: "orchestrator",
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

vi.mock("./ModelPreferences", () => ({ ModelPreferences: () => <p>model preferences</p> }));

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
    expect(screen.getByText("openai")).toBeTruthy();
    expect(screen.getAllByText("codex").length).toBeGreaterThan(0);
    expect(screen.getByText("model preferences")).toBeTruthy();
    expect(screen.getAllByText("no").length).toBeGreaterThan(0);
  });
});
