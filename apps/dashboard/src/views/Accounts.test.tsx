// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  localBrokerNotice,
  nativeAuthNotice,
  PROVIDER_PICK_CUSTOM,
  providerOptionsFromCatalog,
  validateProviderId,
} from "../accountProviders";
import type { EngineCatalog } from "../api";
import { Accounts } from "./Accounts";

const readiness = {
  installed: { ok: true, detail: "", at: null },
  authenticated: { ok: false, detail: "", at: null },
  validated: { ok: null, detail: "", at: null },
};

const catalog: EngineCatalog = {
  engines: [
    {
      id: "codex",
      label: "Codex (OpenAI)",
      role: "orchestrator",
      axes: {
        model_provider: "openai",
        model: null,
        harness: "codex",
        executor: "codex",
        account_provider: "openai",
      },
      auth_methods: ["chatgpt-device-login", "openai-api-key"],
      version: "0.1",
      readiness,
      usable: false,
      accounts: null,
      notes: "",
    },
    {
      id: "claude",
      label: "Claude Code",
      role: "code_executor",
      axes: {
        model_provider: "anthropic",
        model: null,
        harness: "claude",
        executor: "claude",
        account_provider: "anthropic",
      },
      auth_methods: ["argus-account"],
      version: "2",
      readiness,
      usable: false,
      accounts: null,
      notes: "",
    },
    {
      id: "local",
      label: "Local",
      role: "local",
      axes: {
        model_provider: "local",
        model: "qwen",
        harness: "router",
        executor: null,
        account_provider: null,
      },
      auth_methods: [],
      version: null,
      readiness: {
        installed: { ok: true, detail: "", at: null },
        authenticated: { ok: true, detail: "", at: null },
        validated: { ok: true, detail: "", at: null },
      },
      usable: true,
      accounts: null,
      notes: "",
    },
  ],
  local: null,
};

const mock = vi.hoisted(() => ({
  enginesError: undefined as Error | undefined,
  postBody: undefined as Record<string, unknown> | undefined,
}));

vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  return {
    useLiveData: <T,>(load: () => Promise<T>) => {
      const [data, setData] = useState<T>();
      const [error, setError] = useState<Error>();
      useEffect(() => {
        load().then(setData, setError);
      }, []);
      return {
        data,
        error,
        reload: () => load().then(setData, setError),
      };
    },
  };
});

vi.mock("../api", () => ({
  api: {
    GET: (path: string) => path,
    POST: (path: string, init?: { body?: Record<string, unknown> }) => {
      if (path === "/api/accounts") {
        mock.postBody = init?.body;
        return "post-account";
      }
      return path;
    },
    PATCH: () => "patch",
  },
  call: async (path: string) => {
    if (path === "/api/accounts") return [];
    if (path === "/api/accounts/usage") return { accounts: [] };
    if (path === "/api/engines") {
      if (mock.enginesError) throw mock.enginesError;
      return catalog;
    }
    if (path === "post-account") return {};
    return [];
  },
}));

afterEach(() => {
  cleanup();
  mock.enginesError = undefined;
  mock.postBody = undefined;
});

describe("accountProviders", () => {
  it("dedupes providers from account_provider axes and skips local", () => {
    const opts = providerOptionsFromCatalog(catalog);
    expect(opts.map((o) => o.id)).toEqual(["anthropic", "openai"]);
    expect(opts.some((o) => o.id === "codex" || o.id === "claude")).toBe(false);
  });

  it("native auth notice for openai and anthropic", () => {
    expect(nativeAuthNotice("openai")).toMatch(/Codex/);
    expect(nativeAuthNotice("anthropic")).toMatch(/Claude Code/);
    expect(nativeAuthNotice("other")).toBeUndefined();
  });

  it("local broker notice discourages fake accounts", () => {
    expect(localBrokerNotice()).toMatch(/broker/);
  });

  it("rejects custom sentinel and invalid ids", () => {
    expect(validateProviderId(PROVIDER_PICK_CUSTOM)).toBeTruthy();
    expect(validateProviderId("Bad")).toBeTruthy();
    expect(validateProviderId("openai")).toBeNull();
  });
});

describe("Accounts", () => {
  it("renders the accounts table without opening add", async () => {
    render(<Accounts />);
    expect(await screen.findByText("No accounts yet.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reload secrets file" })).toBeTruthy();
  });

  it("submits anthropic provider from catalogue", async () => {
    render(<Accounts />);
    fireEvent.click(screen.getByRole("button", { name: "Add account" }));
    await screen.findByText(localBrokerNotice());
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "a1" } });
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "anthropic" } });
    fireEvent.change(screen.getByLabelText(/Credential/), {
      target: { value: '{"api_key":"x"}' },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(mock.postBody?.provider).toBe("anthropic"));
  });

  it("submits openai provider from catalogue", async () => {
    render(<Accounts />);
    fireEvent.click(screen.getByRole("button", { name: "Add account" }));
    await screen.findByLabelText("Provider");
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "o1" } });
    fireEvent.change(screen.getByLabelText("Provider"), { target: { value: "openai" } });
    expect(await screen.findByText(/ChatGPT login or API key/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/Credential/), {
      target: { value: '{"api_key":"x"}' },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(mock.postBody?.provider).toBe("openai"));
  });

  it("submits a valid custom provider id", async () => {
    render(<Accounts />);
    fireEvent.click(screen.getByRole("button", { name: "Add account" }));
    await screen.findByLabelText("Provider");
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "c1" } });
    fireEvent.change(screen.getByLabelText("Provider"), {
      target: { value: PROVIDER_PICK_CUSTOM },
    });
    fireEvent.change(screen.getByLabelText("Custom provider id"), {
      target: { value: "my_vendor" },
    });
    fireEvent.change(screen.getByLabelText(/Credential/), {
      target: { value: '{"token":"t"}' },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(mock.postBody?.provider).toBe("my_vendor"));
  });

  it("shows catalogue load error with retry", async () => {
    mock.enginesError = new Error("catalog down");
    render(<Accounts />);
    fireEvent.click(screen.getByRole("button", { name: "Add account" }));
    expect(await screen.findByText(/catalog down/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Retry" })).toBeTruthy();
  });
});
