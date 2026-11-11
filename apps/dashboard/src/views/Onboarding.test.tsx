// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OnboardingStatus, TemplateCatalog } from "../api";
import {
  firstIncompleteStep,
  NO_BUILTIN_ACCOUNT,
  OnboardingWizard,
  orchestratorBlockers,
  shouldAutoOpen,
} from "./Onboarding";

function orchestratorCatalog(ready: boolean, missing: string[][] = []): TemplateCatalog {
  return {
    templates: [
      { id: "nanoclaw-orchestrator", ready, readiness: missing.map((m) => ({ missing: m })) },
    ],
    nodes: [],
    public_url_configured: true,
  } as unknown as TemplateCatalog;
}

const mock = vi.hoisted(() => ({
  status: {
    account: false,
    worker: false,
    orchestrator: false,
    dismissed: false,
  } as OnboardingStatus,
  catalog: undefined as unknown as TemplateCatalog,
  reads: [] as string[],
  posts: [] as Record<string, unknown>[],
  put: vi.fn(async () => ({ key: "onboarding.dismissed", value: true, source: "global" })),
}));

vi.mock("../api", () => ({
  api: {
    GET: (path: string) => path,
    PUT: () => "/api/settings/{key}",
    POST: (_: string, init: { body: Record<string, unknown> }) => ({ post: init.body }),
  },
  call: async (path: string | { post: Record<string, unknown> }) => {
    if (typeof path !== "string") {
      mock.posts.push(path.post);
      return {};
    }
    mock.reads.push(path);
    if (path === "/api/engines") return { engines: [], local: null };
    if (path === "/api/onboarding") return { ...mock.status };
    if (path === "/api/settings/{key}") return mock.put();
    if (path === "/api/fleet/templates") {
      return mock.catalog;
    }
    return {};
  },
}));

vi.mock("../live", async () => {
  const { useEffect, useState } = await import("react");
  return {
    useLive: () => ({ status: "live", version: 0 }),
    useLiveData: <T,>(load: () => Promise<T>) => {
      const [data, setData] = useState<T>();
      useEffect(() => {
        load().then(setData);
      }, []);
      return { data, error: undefined, reload: () => load().then(setData) };
    },
  };
});

vi.mock("./ClaudeAccount", () => ({
  ClaudeAccount: ({ onClose }: { onClose: () => void }) => (
    <div role="dialog" aria-label="Add Claude account">
      <button type="button" onClick={onClose}>
        cancel claude
      </button>
    </div>
  ),
}));

vi.mock("./CodexAccount", () => ({
  CodexAccount: ({ onClose }: { onClose: () => void }) => (
    <div role="dialog" aria-label="Add Codex account">
      <button type="button" onClick={onClose}>
        cancel codex
      </button>
    </div>
  ),
}));

vi.mock("./GuidedWorker", () => ({
  NewWorker: () => <div>guided worker form</div>,
}));

afterEach(() => {
  cleanup();
  mock.put.mockClear();
  mock.posts = [];
});

beforeEach(() => {
  mock.catalog = orchestratorCatalog(true);
  mock.status = {
    account: false,
    worker: false,
    orchestrator: false,
    dismissed: false,
  };
});

describe("shouldAutoOpen", () => {
  it("opens when account or worker is missing and not dismissed", () => {
    expect(
      shouldAutoOpen({ account: false, worker: true, orchestrator: false, dismissed: false }),
    ).toBe(true);
    expect(
      shouldAutoOpen({ account: true, worker: false, orchestrator: false, dismissed: false }),
    ).toBe(true);
    expect(
      shouldAutoOpen({ account: true, worker: true, orchestrator: false, dismissed: false }),
    ).toBe(false);
    expect(
      shouldAutoOpen({ account: false, worker: false, orchestrator: false, dismissed: true }),
    ).toBe(false);
  });
});

describe("firstIncompleteStep", () => {
  it("returns the first incomplete step", () => {
    expect(
      firstIncompleteStep({ account: false, worker: false, orchestrator: false, dismissed: false }),
    ).toBe(1);
    expect(
      firstIncompleteStep({ account: true, worker: false, orchestrator: false, dismissed: false }),
    ).toBe(2);
    expect(
      firstIncompleteStep({ account: true, worker: true, orchestrator: false, dismissed: false }),
    ).toBe(3);
  });
});

describe("orchestratorBlockers", () => {
  it("is undefined when the orchestrator template is ready", () => {
    expect(orchestratorBlockers(orchestratorCatalog(true))).toBeUndefined();
  });

  it("lists the missing facts of every readiness entry", () => {
    expect(
      orchestratorBlockers(
        orchestratorCatalog(false, [["no docker socket"], ["image absent", "no gateway"]]),
      ),
    ).toEqual(["no docker socket", "image absent", "no gateway"]);
  });

  it("falls back to a generic message when nothing is listed", () => {
    expect(orchestratorBlockers(orchestratorCatalog(false))).toEqual([
      "The internal orchestrator template is not ready.",
    ]);
  });
});

describe("OnboardingWizard", () => {
  it("reads nothing while closed", async () => {
    mock.reads.length = 0;
    render(<OnboardingWizard open={false} onClose={() => {}} />);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(mock.reads).toEqual([]);
  });

  it("shows account actions and opens Claude form", async () => {
    render(<OnboardingWizard open onClose={() => {}} />);
    expect(await screen.findByRole("dialog", { name: "Set up Argus" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Add Claude account" }));
    expect(screen.getByRole("dialog", { name: "Add Claude account" })).toBeTruthy();
  });

  it("dismisses via the setting on close", async () => {
    const onClose = vi.fn();
    render(<OnboardingWizard open onClose={onClose} />);
    await screen.findByRole("dialog", { name: "Set up Argus" });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    await waitFor(() => expect(mock.put).toHaveBeenCalled());
    expect(onClose).toHaveBeenCalled();
  });

  it("advances past a done account step to the worker form", async () => {
    mock.status = { account: true, worker: false, orchestrator: false, dismissed: false };
    render(<OnboardingWizard open initialStep={1} onClose={() => {}} />);
    expect(await screen.findByText("guided worker form")).toBeTruthy();
  });

  it("shows the orchestrator step when account and worker are done", async () => {
    mock.status = { account: true, worker: true, orchestrator: false, dismissed: false };
    render(<OnboardingWizard open initialStep={3} onClose={() => {}} />);
    expect(await screen.findByRole("button", { name: "Enable orchestrator" })).toBeTruthy();
    expect(screen.getByLabelText(/Daily budget/)).toBeTruthy();
  });

  it("enables the orchestrator with the daily budget", async () => {
    mock.status = { account: true, worker: true, orchestrator: false, dismissed: false };
    render(<OnboardingWizard open initialStep={3} onClose={() => {}} />);
    await screen.findByRole("button", { name: "Enable orchestrator" });
    fireEvent.click(screen.getByRole("button", { name: "Enable orchestrator" }));
    await waitFor(() => expect(mock.put).toHaveBeenCalled());
    expect(mock.put.mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it("shows the blockers instead of the enable form when the template is not ready", async () => {
    mock.status = { account: true, worker: true, orchestrator: false, dismissed: false };
    mock.catalog = orchestratorCatalog(false, [["no docker socket"]]);
    render(<OnboardingWizard open initialStep={3} onClose={() => {}} />);
    expect(await screen.findByText("no docker socket")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Enable orchestrator" })).toBeNull();
    expect(screen.queryByLabelText(/Daily budget/)).toBeNull();
    expect(mock.put).not.toHaveBeenCalled();
  });

  it("offers the built-in runtime with no template check", async () => {
    mock.status = {
      account: true,
      worker: false,
      orchestrator: false,
      dismissed: false,
      orchestrator_runtime: "builtin",
      builtin_account: "anthropic-key",
    };
    mock.catalog = orchestratorCatalog(false, [["no docker socket"]]);
    render(<OnboardingWizard open initialStep={3} onClose={() => {}} />);
    expect(await screen.findByRole("button", { name: "Enable orchestrator" })).toBeTruthy();
    expect(screen.getByText("anthropic-key")).toBeTruthy();
    expect(screen.queryByText("no docker socket")).toBeNull();
    expect(screen.getByText(/wait until a worker exists/)).toBeTruthy();
  });

  it("names both reasons when neither runtime can run", async () => {
    mock.status = {
      account: false,
      worker: false,
      orchestrator: false,
      dismissed: false,
      orchestrator_runtime: null,
    };
    mock.catalog = orchestratorCatalog(false, [["no docker socket"]]);
    render(<OnboardingWizard open initialStep={3} onClose={() => {}} />);
    expect(await screen.findByText("no docker socket")).toBeTruthy();
    expect(screen.getByText(NO_BUILTIN_ACCOUNT)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /step 1/ }));
    expect(await screen.findByRole("button", { name: "Add local model" })).toBeTruthy();
  });

  it("offers Copilot as executor-only: it imports as a subscription and never completes step 1", async () => {
    render(<OnboardingWizard open onClose={() => {}} />);
    await screen.findByRole("dialog", { name: "Set up Argus" });
    expect(screen.getByText(/Copilot account is executor-only/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Add Copilot account" }));
    const provider = (await screen.findByLabelText("Provider")) as HTMLSelectElement;
    await waitFor(() => expect(provider.value).toBe("copilot"));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "seat" } });
    const token = screen.getByLabelText("Copilot token");
    fireEvent.change(token, { target: { value: "ghp_classicsecret" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByText(/are not accepted/)).toBeTruthy();
    expect(mock.posts).toEqual([]);
    fireEvent.change(token, { target: { value: "github_pat_abc" } });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() => expect(screen.queryByLabelText("Provider")).toBeNull());
    expect(mock.posts).toMatchObject([
      { provider: "copilot", kind: "subscription", credential: { token: "github_pat_abc" } },
    ]);
    // The server still reports account=false: step 1 stays current and not done.
    const tab = screen.getByRole("button", { name: /Add an account/ });
    expect(tab.getAttribute("aria-current")).toBe("step");
    expect(tab.textContent).not.toContain("✓");
    expect(screen.getByRole("button", { name: "Add Copilot account" })).toBeTruthy();
  });

  it("hides the Copilot button once the account step is done", async () => {
    mock.status = { account: true, worker: false, orchestrator: false, dismissed: false };
    render(<OnboardingWizard open initialStep={1} onClose={() => {}} />);
    await screen.findByText("guided worker form");
    fireEvent.click(screen.getByRole("button", { name: /Add an account/ }));
    expect(screen.queryByRole("button", { name: "Add Copilot account" })).toBeNull();
  });
});
