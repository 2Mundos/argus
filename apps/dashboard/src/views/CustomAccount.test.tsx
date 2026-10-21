// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CustomAccount } from "./CustomAccount";

const mock = vi.hoisted(() => ({
  posts: [] as { path: string; body?: unknown }[],
  patches: [] as { path: string; body?: unknown; id?: unknown }[],
}));

vi.mock("../api", () => ({
  api: {
    POST: (path: string, init?: { body?: unknown; params?: unknown }) => {
      mock.posts.push({ path, body: init?.body });
      return `post:${path}`;
    },
    PATCH: (path: string, init?: { body?: unknown; params?: { path?: unknown } }) => {
      mock.patches.push({ path, body: init?.body, id: init?.params?.path });
      return `patch:${path}`;
    },
  },
  call: async (key: string) => {
    if (key === "post:/api/accounts") return { id: "acc_1" };
    if (key === "post:/api/accounts/{account_id}/probe") {
      return {
        status: "ok",
        reason: "ok",
        models: ["openai/gpt-5", "anthropic/claude-sonnet-4"],
      };
    }
    if (key === "patch:/api/accounts/{account_id}") return { id: "acc_1" };
    throw new Error(`unexpected ${key}`);
  },
}));

afterEach(() => {
  cleanup();
  mock.posts = [];
  mock.patches = [];
});

beforeEach(() => {
  mock.posts = [];
  mock.patches = [];
});

describe("CustomAccount", () => {
  it("lets you pick an OpenRouter harness model after probe and PATCHes config", async () => {
    const onDone = vi.fn();
    render(<CustomAccount onClose={() => undefined} onDone={onDone} />);
    fireEvent.change(screen.getByLabelText(/^API key/), { target: { value: "sk-test" } });
    fireEvent.click(screen.getByRole("button", { name: "Save and check" }));
    expect(await screen.findByText(/2 models/)).toBeTruthy();
    const harness = screen.getByRole("group", { name: "NanoClaw harness" });
    const model = harness.querySelector("input") as HTMLInputElement;
    const listId = model.getAttribute("list");
    expect(listId).toBeTruthy();
    const options = [...document.querySelectorAll(`#${listId} option`)].map((o) =>
      o.getAttribute("value"),
    );
    expect(options).toEqual(["openai/gpt-5", "anthropic/claude-sonnet-4"]);
    fireEvent.change(model, { target: { value: "anthropic/claude-sonnet-4" } });
    fireEvent.click(screen.getByRole("button", { name: "Save preferences" }));
    await waitFor(() => expect(mock.patches).toHaveLength(1));
    expect(mock.patches[0]?.body).toEqual({
      config: {
        base_url: "https://openrouter.ai/api/v1",
        label: "openrouter",
        engine: { model: "anthropic/claude-sonnet-4" },
        executor: {},
      },
    });
    expect(onDone).toHaveBeenCalled();
  });
});
