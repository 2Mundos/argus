// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { AuthFacts } from "./AuthFacts";
import type { Account } from "./api";

afterEach(cleanup);

const account = (patch: Partial<Account>) =>
  ({
    credential: { state: "present" },
    last_accepted_at: null,
    last_rejected_at: null,
    ...patch,
  }) as Account;

describe("AuthFacts", () => {
  it("shows the three facts separately and flags a newer rejection", () => {
    render(
      <AuthFacts
        account={account({
          credential: { state: "expired", expires_at: "2026-01-01T09:00:00Z" },
          last_accepted_at: "2026-01-01T10:00:00Z",
          last_rejected_at: "2026-01-01T11:00:00Z",
        })}
      />,
    );
    expect(screen.getByText("expired")).toBeTruthy();
    expect(screen.getByText(/Last accepted/)).toBeTruthy();
    expect(screen.getByText(/Last rejected/)).toBeTruthy();
    expect(screen.getByText(/newer than last accepted/)).toBeTruthy();
  });

  it("says never/none for an account with no evidence, and never healthy", () => {
    const { container } = render(<AuthFacts account={account({})} />);
    expect(screen.getByText("never")).toBeTruthy();
    expect(screen.getByText("none")).toBeTruthy();
    expect(container.textContent?.toLowerCase()).not.toContain("healthy");
  });

  it("renders nothing for an account without a model credential", () => {
    const { container } = render(<AuthFacts account={account({ credential: null })} />);
    expect(container.textContent).toBe("");
  });
});
