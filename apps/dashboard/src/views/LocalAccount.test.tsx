// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LocalAccount } from "./LocalAccount";

const mock = vi.hoisted(() => ({
  detection: { candidates: [], hint: null } as unknown,
  posted: [] as unknown[],
}));

vi.mock("../api", () => ({
  api: {
    POST: (path: string, init?: { body?: unknown }) => ({ path, body: init?.body }),
  },
  call: async ({ path, body }: { path: string; body?: unknown }) => {
    if (path === "/api/accounts/local/detect") return mock.detection;
    mock.posted.push(body);
    return {};
  },
}));

afterEach(() => {
  cleanup();
  mock.posted.length = 0;
});

describe("LocalAccount", () => {
  it("picks the endpoint workers can reach and saves a local account", async () => {
    mock.detection = {
      candidates: [
        {
          base_url: "http://127.0.0.1:11434",
          server_hint: "ollama",
          models: ["llama3"],
          preferred: false,
          note: "workers cannot reach this address",
        },
        {
          base_url: "http://host.docker.internal:11434",
          server_hint: "ollama",
          models: ["qwen3:8b", "llama3"],
          preferred: true,
          note: null,
        },
      ],
      hint: null,
    };
    const onDone = vi.fn();
    render(<LocalAccount onClose={() => {}} onDone={onDone} />);
    fireEvent.click(screen.getByRole("button", { name: "Detect" }));
    expect(await screen.findByDisplayValue("local-qwen3:8b")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(mock.posted).toEqual([
      {
        name: "local-qwen3:8b",
        provider: "local",
        kind: "api_key",
        credential: { base_url: "http://host.docker.internal:11434", model: "qwen3:8b" },
      },
    ]);
  });

  it("shows the hint when nothing answers, and saves nothing", async () => {
    mock.detection = { candidates: [], hint: "Argus runs in a container." };
    render(<LocalAccount onClose={() => {}} onDone={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: "Detect" }));
    expect(await screen.findByText(/Argus runs in a container\./)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });
});
