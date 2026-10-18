// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Account } from "../api";
import { ClaudeAccount } from "./ClaudeAccount";

const OAT = `sk-ant-${"oat01-"}${"x".repeat(40)}`;
const API = `sk-ant-${"api03-"}${"y".repeat(40)}`;
const CODE = "AbC-123_xyz#state";

const mock = vi.hoisted(() => ({
  posts: [] as Array<{ path: string; body: unknown }>,
  puts: [] as Array<{ path: string; id: unknown; body: unknown }>,
  deletes: [] as string[],
  gets: [] as string[],
  states: [] as Array<Record<string, unknown>>,
  startResult: undefined as Record<string, unknown> | undefined,
  codeResult: undefined as Record<string, unknown> | undefined,
  postError: undefined as Error | undefined,
}));

const pending = (over: Record<string, unknown> = {}) => ({
  login_id: "L1",
  state: "pending",
  phase: "waiting_for_code",
  url: "https://claude.ai/oauth/authorize?state=ST",
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
    PUT: (path: string, init?: { params?: { path?: unknown }; body?: unknown }) => {
      mock.puts.push({ path, id: init?.params?.path, body: init?.body });
      return "put";
    },
    DELETE: (path: string) => {
      mock.deletes.push(path);
      return `delete:${path}`;
    },
  },
  call: async (key: string) => {
    if (key.startsWith("post:") && mock.postError) throw mock.postError;
    if (key === "post:/api/accounts/claude-login") return mock.startResult ?? pending();
    if (key === "post:/api/accounts/claude-login/{login_id}/code") {
      return mock.codeResult ?? pending({ phase: "finishing" });
    }
    if (key.startsWith("get:")) return mock.states.shift() ?? pending();
    if (key.startsWith("delete:")) return pending({ state: "cancelled", url: null, phase: null });
    return {};
  },
}));

const existing = {
  id: "acc-1",
  name: "laude",
  provider: "anthropic",
  kind: "api_key",
} as Account;

beforeEach(() => {
  mock.posts = [];
  mock.puts = [];
  mock.deletes = [];
  mock.gets = [];
  mock.states = [];
  mock.startResult = undefined;
  mock.codeResult = undefined;
  mock.postError = undefined;
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const tab = (name: string) => fireEvent.click(screen.getByRole("tab", { name }));
const nativeFile = JSON.stringify({
  claudeAiOauth: {
    accessToken: "a-SECRET",
    refreshToken: "r-SECRET",
    expiresAt: Date.now() + 3_600_000,
  },
});

describe("three paths", () => {
  it("offers the three choices", () => {
    render(<ClaudeAccount onClose={vi.fn()} onDone={vi.fn()} />);
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual([
      "Import credential file",
      "Use a token or API key",
      "Sign in with Claude",
    ]);
  });
});

describe("import path", () => {
  it("imports Claude's credentials JSON as a subscription account, hiding values", async () => {
    const onDone = vi.fn();
    render(<ClaudeAccount onClose={vi.fn()} onDone={onDone} />);
    const btn = screen.getByRole("button", { name: "Import" }) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Credential content"), {
      target: { value: nativeFile },
    });
    expect(
      await screen.findByText(/Claude login file detected, subscription account/),
    ).toBeTruthy();
    expect(screen.getByRole("status").textContent).not.toContain("SECRET");
    fireEvent.click(btn);
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(mock.posts).toEqual([
      {
        path: "/api/accounts",
        body: {
          name: "Claude (imported)",
          provider: "anthropic",
          kind: "subscription",
          credential: JSON.parse(nativeFile),
        },
      },
    ]);
  });

  it("reads a chosen file", async () => {
    render(<ClaudeAccount onClose={vi.fn()} onDone={vi.fn()} />);
    const file = new File([nativeFile], "credentials.json", { type: "application/json" });
    fireEvent.change(screen.getByLabelText("Credential file"), { target: { files: [file] } });
    expect(await screen.findByText(/credentials\.json: Claude login file detected/)).toBeTruthy();
  });

  it("rejects a bad or expired document without calling the API or echoing it", async () => {
    render(<ClaudeAccount onClose={vi.fn()} onDone={vi.fn()} />);
    const area = screen.getByLabelText("Credential content");
    fireEvent.change(area, {
      target: { value: '{"claudeAiOauth":{"accessToken":"a-SECRET"}}' },
    });
    expect(await screen.findByText(/missing: refreshToken/)).toBeTruthy();
    fireEvent.change(area, {
      target: {
        value: JSON.stringify({
          claudeAiOauth: { accessToken: "a-SECRET", refreshToken: "r-SECRET", expiresAt: 1000 },
        }),
      },
    });
    expect(await screen.findByText(/in the past/)).toBeTruthy();
    expect(document.querySelector(".error")?.textContent).not.toContain("SECRET");
    expect((screen.getByRole("button", { name: "Import" }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    expect(mock.posts).toEqual([]);
  });

  it("shows a server refusal and keeps the dialog", async () => {
    mock.postError = new Error("invalid anthropic credential: needs one of");
    const onDone = vi.fn();
    render(<ClaudeAccount onClose={vi.fn()} onDone={onDone} />);
    fireEvent.change(screen.getByLabelText("Credential content"), {
      target: { value: nativeFile },
    });
    fireEvent.click(screen.getByRole("button", { name: "Import" }));
    expect(await screen.findByText(/invalid anthropic credential/)).toBeTruthy();
    expect(onDone).not.toHaveBeenCalled();
  });
});

describe("token path", () => {
  it("sends the chosen type and derives the kind", async () => {
    const onDone = vi.fn();
    render(<ClaudeAccount onClose={vi.fn()} onDone={onDone} />);
    tab("Use a token or API key");
    fireEvent.change(screen.getByLabelText("Token"), { target: { value: ` ${OAT} ` } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(mock.posts[0]?.body).toEqual({
      name: "Claude (token)",
      provider: "anthropic",
      kind: "subscription",
      credential: { oauth_token: OAT },
    });
  });

  it("treats an API key as an api_key account and refuses a mismatched type", async () => {
    render(<ClaudeAccount onClose={vi.fn()} onDone={vi.fn()} />);
    tab("Use a token or API key");
    fireEvent.change(screen.getByLabelText("Token"), { target: { value: API } });
    expect(await screen.findByText(/holds an API key; use api_key/)).toBeTruthy();
    expect(document.querySelector(".error")?.textContent).not.toContain(API);
    fireEvent.change(screen.getByLabelText("Token type"), { target: { value: "api_key" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(mock.posts).toHaveLength(1));
    expect(mock.posts[0]?.body).toMatchObject({ kind: "api_key", credential: { api_key: API } });
  });

  it("keeps the token in a password field", () => {
    render(<ClaudeAccount onClose={vi.fn()} onDone={vi.fn()} />);
    tab("Use a token or API key");
    expect(screen.getByLabelText("Token").getAttribute("type")).toBe("password");
  });
});

describe("renewal", () => {
  it("replaces the credential of the chosen account and creates nothing", async () => {
    const onDone = vi.fn();
    render(<ClaudeAccount renew={existing} onClose={vi.fn()} onDone={onDone} />);
    expect(screen.getByRole("dialog").getAttribute("aria-label")).toBe("Renew Claude account");
    expect(screen.getByText(/“laude”/)).toBeTruthy();
    expect(screen.queryByLabelText("Name")).toBeNull(); // the name stays
    tab("Use a token or API key");
    fireEvent.change(screen.getByLabelText("Token"), { target: { value: OAT } });
    fireEvent.click(screen.getByRole("button", { name: "Renew" }));
    await waitFor(() => expect(onDone).toHaveBeenCalled());
    expect(mock.posts).toEqual([]);
    expect(mock.puts).toEqual([
      {
        path: "/api/accounts/{account_id}/credential",
        id: { account_id: "acc-1" },
        body: { credential: { oauth_token: OAT } },
      },
    ]);
  });

  it("starts the dedicated login for the account being renewed", async () => {
    render(<ClaudeAccount renew={existing} onClose={vi.fn()} onDone={vi.fn()} />);
    tab("Sign in with Claude");
    fireEvent.click(screen.getByRole("button", { name: "Start login" }));
    await screen.findByLabelText("Code from the sign-in page");
    expect(mock.posts).toEqual([
      { path: "/api/accounts/claude-login", body: { renew_account_id: "acc-1" } },
    ]);
  });
});

describe("dedicated login path", () => {
  const start = async () => {
    tab("Sign in with Claude");
    fireEvent.click(screen.getByRole("button", { name: "Start login" }));
    await screen.findByLabelText("Code from the sign-in page");
  };

  it("shows the link, forwards the code, polls and finishes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const onDone = vi.fn();
    render(<ClaudeAccount onClose={vi.fn()} onDone={onDone} />);
    await start();
    const link = screen.getByRole("link", { name: "the Claude sign-in page" });
    expect(link.getAttribute("href")).toBe("https://claude.ai/oauth/authorize?state=ST");
    expect(link.getAttribute("rel")).toContain("noopener");
    const input = screen.getByLabelText("Code from the sign-in page") as HTMLInputElement;
    expect(input.type).toBe("password");
    fireEvent.change(input, { target: { value: `  ${CODE}\n` } });
    fireEvent.click(screen.getByRole("button", { name: "Send code" }));
    await screen.findByText(/Code sent, finishing sign-in/);
    expect(mock.posts.at(-1)).toEqual({
      path: "/api/accounts/claude-login/{login_id}/code",
      body: { code: CODE },
    });
    expect(screen.queryByLabelText("Code from the sign-in page")).toBeNull();
    mock.states = [
      pending({ phase: "finishing" }),
      pending({ state: "completed", url: null, phase: null, account_id: "A1" }),
    ];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });
    expect(onDone).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2100);
    });
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
    expect(document.body.textContent).not.toContain(CODE);
  });

  it("explains a refused code and takes another", async () => {
    render(<ClaudeAccount onClose={vi.fn()} onDone={vi.fn()} />);
    await start();
    mock.codeResult = pending({ refusal: "oauth_400", attempts: 1, max_attempts: 3 });
    const input = () => screen.getByLabelText("Code from the sign-in page") as HTMLInputElement;
    fireEvent.change(input(), { target: { value: CODE } });
    fireEvent.click(screen.getByRole("button", { name: "Send code" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("came from an earlier link");
    expect(alert.textContent).toContain("1 of 3 tries used");
    expect(document.body.textContent).toContain("including the part after #");
    expect(document.body.textContent).not.toContain(CODE);
    fireEvent.change(input(), { target: { value: "Other-code#st" } });
    fireEvent.click(screen.getByRole("button", { name: "Send code" }));
    await waitFor(() => expect(mock.posts.filter((p) => p.path.endsWith("/code"))).toHaveLength(2));
  });

  it("hides the code field while Claude prepares the new link, then shows the new link", async () => {
    render(<ClaudeAccount onClose={vi.fn()} onDone={vi.fn()} />);
    await start();
    mock.codeResult = pending({ phase: "renewing_link", url: null, refusal: "oauth_400" });
    fireEvent.change(screen.getByLabelText("Code from the sign-in page"), {
      target: { value: CODE },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send code" }));
    expect(await screen.findByText(/Preparing a new sign-in link/)).toBeTruthy();
    expect(screen.queryByLabelText("Code from the sign-in page")).toBeNull();
  });

  it("refuses a code without the #state part locally", async () => {
    render(<ClaudeAccount onClose={vi.fn()} onDone={vi.fn()} />);
    await start();
    fireEvent.change(screen.getByLabelText("Code from the sign-in page"), {
      target: { value: "onlythefirstpart" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send code" }));
    expect(await screen.findByText(/part after #/)).toBeTruthy();
    expect(mock.posts.some((p) => p.path.endsWith("/code"))).toBe(false);
  });

  it("refuses a malformed code locally and does not send it", async () => {
    render(<ClaudeAccount onClose={vi.fn()} onDone={vi.fn()} />);
    await start();
    fireEvent.change(screen.getByLabelText("Code from the sign-in page"), {
      target: { value: "bad code;$(x)" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Send code" }));
    expect(await screen.findByText(/not a valid code/)).toBeTruthy();
    expect(document.body.textContent).not.toContain("$(x)");
    expect(mock.posts.some((p) => p.path.endsWith("/code"))).toBe(false);
  });

  it("cancels a pending login on the server, once", async () => {
    const onClose = vi.fn();
    const view = render(<ClaudeAccount onClose={onClose} onDone={vi.fn()} />);
    await start();
    fireEvent.click(screen.getByRole("button", { name: "Cancel login" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    view.unmount();
    expect(mock.deletes).toEqual(["/api/accounts/claude-login/{login_id}"]);
  });

  it("cleans up a pending login when the wizard is closed any other way", async () => {
    const view = render(<ClaudeAccount onClose={vi.fn()} onDone={vi.fn()} />);
    await start();
    view.unmount();
    expect(mock.deletes).toEqual(["/api/accounts/claude-login/{login_id}"]);
  });

  it("never links a foreign URL", async () => {
    mock.startResult = pending({ url: "https://evil.example/oauth" });
    render(<ClaudeAccount onClose={vi.fn()} onDone={vi.fn()} />);
    await start();
    expect(screen.queryByRole("link")).toBeNull();
    expect(screen.getByText(/the link was refused/)).toBeTruthy();
  });

  it("offers a retry after an expiry or failure and stores nothing", async () => {
    mock.startResult = pending({ state: "expired", error: "expired", url: null, phase: null });
    render(<ClaudeAccount onClose={vi.fn()} onDone={vi.fn()} />);
    tab("Sign in with Claude");
    fireEvent.click(screen.getByRole("button", { name: "Start login" }));
    expect((await screen.findByRole("alert")).textContent).toBe(
      "Login expired (expired). Nothing was stored.",
    );
    mock.startResult = undefined;
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByRole("button", { name: "Start login" })).toBeTruthy();
  });

  it("shows why a login could not start", async () => {
    mock.postError = new Error("no Claude client is configured");
    render(<ClaudeAccount onClose={vi.fn()} onDone={vi.fn()} />);
    tab("Sign in with Claude");
    fireEvent.click(screen.getByRole("button", { name: "Start login" }));
    expect(await screen.findByText(/no Claude client is configured/)).toBeTruthy();
  });
});
