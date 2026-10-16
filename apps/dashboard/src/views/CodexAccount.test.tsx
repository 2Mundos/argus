// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CodexAccount } from "./CodexAccount";

const SECRET = "refresh-SECRET";
const authJson = JSON.stringify({
  auth_mode: "chatgpt",
  tokens: { id_token: "i-SECRET", access_token: "a-SECRET", refresh_token: SECRET },
});

const mock = vi.hoisted(() => ({
  posts: [] as Array<{ path: string; body: unknown }>,
  deletes: [] as string[],
  gets: [] as string[],
  states: [] as Array<Record<string, unknown>>,
  startResult: undefined as Record<string, unknown> | undefined,
  postError: undefined as Error | undefined,
}));

const pending = (over: Record<string, unknown> = {}) => ({
  login_id: "L1",
  state: "pending",
  verification_url: "https://auth.openai.com/codex/device",
  user_code: "ABCD-1234",
  expires_in_s: 840,
  account_id: null,
  error: null,
  ...over,
});

vi.mock("../api", () => ({
  api: {
    GET: (path: string) => {
      mock.gets.push(path);
      return `get:${path}`;
    },
    POST: (path: string, init?: { body?: unknown }) => {
      mock.posts.push({ path, body: init?.body });
      return `post:${path}`;
    },
    DELETE: (path: string) => {
      mock.deletes.push(path);
      return `delete:${path}`;
    },
  },
  call: async (key: string) => {
    if (key.startsWith("post:") && mock.postError) throw mock.postError;
    if (key === "post:/api/accounts/codex-login") return mock.startResult ?? pending();
    if (key.startsWith("get:")) return mock.states.shift() ?? pending();
    if (key.startsWith("delete:")) return pending({ state: "cancelled", user_code: null });
    return {};
  },
}));

beforeEach(() => {
  mock.posts = [];
  mock.deletes = [];
  mock.gets = [];
  mock.states = [];
  mock.startResult = undefined;
  mock.postError = undefined;
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("import path", () => {
  it("accepts a ChatGPT auth.json, shows no values and posts an openai subscription", async () => {
    const onDone = vi.fn();
    render(<CodexAccount onClose={vi.fn()} onDone={onDone} />);
    const importBtn = screen.getByRole("button", { name: "Import" }) as HTMLButtonElement;
    expect(importBtn.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("auth.json content"), { target: { value: authJson } });
    expect(await screen.findByText(/ChatGPT login detected/)).toBeTruthy();
    // Only the user's own textarea holds the document; nothing else repeats a value.
    expect(screen.getByRole("status").textContent).not.toContain("SECRET");
    fireEvent.click(importBtn);
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(mock.posts).toEqual([
      {
        path: "/api/accounts",
        body: {
          name: "Codex (imported)",
          provider: "openai",
          kind: "subscription",
          credential: JSON.parse(authJson),
        },
      },
    ]);
  });

  it("reads a chosen file", async () => {
    render(<CodexAccount onClose={vi.fn()} onDone={vi.fn()} />);
    const file = new File([authJson], "auth.json", { type: "application/json" });
    fireEvent.change(screen.getByLabelText("auth.json file"), { target: { files: [file] } });
    expect(await screen.findByText(/auth\.json: ChatGPT login detected/)).toBeTruthy();
  });

  it("rejects a bad document without calling the API or echoing it", async () => {
    render(<CodexAccount onClose={vi.fn()} onDone={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("auth.json content"), {
      target: { value: '{"auth_mode":"chatgpt","tokens":{"access_token":"a-SECRET"}}' },
    });
    expect(await screen.findByText(/missing: id_token, refresh_token/)).toBeTruthy();
    expect(document.querySelector(".error")?.textContent).not.toContain("SECRET");
    expect((screen.getByRole("button", { name: "Import" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(mock.posts).toEqual([]);
  });

  it("shows a server refusal and keeps the dialog", async () => {
    mock.postError = new Error("invalid openai credential: tokens is missing: id_token");
    const onDone = vi.fn();
    render(<CodexAccount onClose={vi.fn()} onDone={onDone} />);
    fireEvent.change(screen.getByLabelText("auth.json content"), { target: { value: authJson } });
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    expect(await screen.findByText(/invalid openai credential/)).toBeTruthy();
    expect(onDone).not.toHaveBeenCalled();
  });
});

describe("dedicated login path", () => {
  const openLogin = () =>
    fireEvent.click(screen.getByRole("tab", { name: "Generate with a dedicated login" }));

  it("shows the link and code, polls, and finishes when the account exists", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onDone = vi.fn();
    render(<CodexAccount onClose={vi.fn()} onDone={onDone} />);
    openLogin();
    fireEvent.click(screen.getByRole("button", { name: "Start login" }));
    const link = await screen.findByRole("link", { name: "the OpenAI sign-in page" });
    expect(link.getAttribute("href")).toBe("https://auth.openai.com/codex/device");
    expect(link.getAttribute("rel")).toContain("noopener");
    expect(screen.getByLabelText("One-time code").textContent).toBe("ABCD-1234");
    mock.states = [
      pending({ expires_in_s: 800 }),
      pending({ state: "completed", user_code: null, verification_url: null, account_id: "A1" }),
    ];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });
    expect(onDone).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
    expect(mock.gets.every((p) => p === "/api/accounts/codex-login/{login_id}")).toBe(true);
    expect(screen.queryByLabelText("One-time code")).toBeNull();
  });

  it("cancels a pending login on the server", async () => {
    const onClose = vi.fn();
    render(<CodexAccount onClose={onClose} onDone={vi.fn()} />);
    openLogin();
    fireEvent.click(screen.getByRole("button", { name: "Start login" }));
    await screen.findByLabelText("One-time code");
    fireEvent.click(screen.getByRole("button", { name: "Cancel login" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(mock.deletes).toEqual(["/api/accounts/codex-login/{login_id}"]);
  });

  it("never links a foreign verification URL", async () => {
    mock.startResult = pending({ verification_url: "https://evil.example/device" });
    render(<CodexAccount onClose={vi.fn()} onDone={vi.fn()} />);
    openLogin();
    fireEvent.click(screen.getByRole("button", { name: "Start login" }));
    await screen.findByLabelText("One-time code");
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText(/the link was refused/)).toBeTruthy();
  });

  it("reports a failed or expired login as storing nothing", async () => {
    mock.startResult = pending({ state: "expired", error: "expired", user_code: null });
    render(<CodexAccount onClose={vi.fn()} onDone={vi.fn()} />);
    openLogin();
    fireEvent.click(screen.getByRole("button", { name: "Start login" }));
    expect(await screen.findByRole("alert")).toHaveProperty(
      "textContent",
      "Login expired (expired). Nothing was stored.",
    );
  });

  it("shows why a login could not start", async () => {
    mock.postError = new Error("no Codex client is configured");
    render(<CodexAccount onClose={vi.fn()} onDone={vi.fn()} />);
    openLogin();
    fireEvent.click(screen.getByRole("button", { name: "Start login" }));
    expect(await screen.findByText(/no Codex client is configured/)).toBeTruthy();
  });
});
