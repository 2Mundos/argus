// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { OnboardingStatus } from "../api";
import { firstIncompleteStep, OnboardingWizard, shouldAutoOpen } from "./Onboarding";

const mock = vi.hoisted(() => ({
  status: {
    account: false,
    worker: false,
    orchestrator: false,
    dismissed: false,
  } as OnboardingStatus,
  put: vi.fn(async () => ({ key: "onboarding.dismissed", value: true, source: "global" })),
}));

vi.mock("../api", () => ({
  api: {
    GET: (path: string) => path,
    PUT: () => "/api/settings/{key}",
  },
  call: async (path: string) => {
    if (path === "/api/onboarding") return { ...mock.status };
    if (path === "/api/settings/{key}") return mock.put();
    if (path === "/api/fleet/templates") {
      return { templates: [], nodes: [], public_url_configured: true };
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
});

beforeEach(() => {
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

describe("OnboardingWizard", () => {
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
});
