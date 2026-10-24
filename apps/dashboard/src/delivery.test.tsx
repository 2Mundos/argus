// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { Delivery } from "./api";
import { TaskDelivery } from "./delivery";

afterEach(cleanup);

const base: Delivery = {
  pr: "https://git.example.net/acme/app/pulls/7",
  pr_state: "open",
  merged_at: null,
  checked_at: "2026-10-24T10:00:00Z",
  flags: [],
  accept_decision_id: null,
  accept_blocked: null,
};

describe("TaskDelivery", () => {
  it("shows unknown as its own state, never as open", () => {
    render(<TaskDelivery delivery={{ ...base, pr_state: "unknown" }} />);
    expect(screen.getByText("unknown (not read)")).toBeTruthy();
    expect(screen.queryByText("open")).toBeNull();
  });

  it("flags a merge as pending review and offers accept only with a decision", () => {
    const merged = {
      ...base,
      pr_state: "merged" as const,
      flags: ["delivery_merged_pending_review" as const],
    };
    const { rerender } = render(
      <TaskDelivery delivery={{ ...merged, accept_blocked: "checklist_pending" }} />,
    );
    expect(screen.getByText(/Merged, pending review/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.getByText(/merged is not accepted/)).toBeTruthy();

    rerender(<TaskDelivery delivery={{ ...merged, accept_decision_id: "d1" }} />);
    expect(screen.getByRole("button", { name: "Accept" })).toBeTruthy();
  });

  it("shows no merge flag for an open PR", () => {
    render(<TaskDelivery delivery={base} />);
    expect(screen.queryByText(/pending review/)).toBeNull();
  });
});
