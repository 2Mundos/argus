// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { TaskFailover } from "./failover";

afterEach(cleanup);

const NOW = Date.parse("2026-10-20T20:00:00Z");
const base = { state: "ready" as const, block_reason: null, refill_at: null };

const renderDl = (task: Parameters<typeof TaskFailover>[0]["task"]) =>
  render(
    <dl>
      <TaskFailover task={task} now={NOW} />
    </dl>,
  );

describe("TaskFailover", () => {
  it("shows the serving account and the last switch with the literal reason", () => {
    renderDl({
      ...base,
      serving_account: { id: "a-b", name: "acct-b", provider: "anthropic" },
      failover: {
        from_account: "acct-a",
        to_account: "acct-b",
        reason: "Rate limit [five_hour] (resets 2026-10-20T23:50:00Z)",
        at: "2026-10-20T19:58:00Z",
        attempt: 1,
      },
    });
    expect(screen.getByText(/acct-a → acct-b/)).toBeTruthy();
    expect(screen.getByText("Rate limit [five_hour] (resets 2026-10-20T23:50:00Z)")).toBeTruthy();
    expect(screen.getByText("(anthropic)")).toBeTruthy();
  });

  it("shows when an awaiting-quota task will be tried again", () => {
    renderDl({
      ...base,
      state: "blocked",
      block_reason: "awaiting_quota",
      refill_at: "2026-10-20T23:50:00Z",
      serving_account: null,
      failover: {
        from_account: "acct-a",
        to_account: null,
        reason: "Rate limit [five_hour]",
        at: "2026-10-20T19:58:00Z",
        attempt: 1,
      },
    });
    expect(screen.getByText("Waiting for quota")).toBeTruthy();
    expect(screen.getByText("until")).toBeTruthy();
    expect(screen.getByTitle("2026-10-20T23:50:00.000Z")).toBeTruthy();
    expect(screen.getByText(/acct-a → no account left/)).toBeTruthy();
  });

  it("separates the codes, keeps an ambiguous reset as words and shows a failed switch", () => {
    renderDl({
      ...base,
      serving_account: null,
      failover: {
        from_account: "codex-1",
        to_account: "codex-2",
        reason: "You\u2019ve hit your usage limit.",
        reason_code: "quota_exhausted",
        reset_status: "ambiguous",
        reset_text: "Nov 19th, 2026 2:08 PM",
        source: "notice",
        outcome: "switch_failed",
        apply: "failed",
        apply_error: "previous runtime did not stop",
        at: "2026-10-20T19:58:00Z",
        attempt: 1,
      },
    });
    expect(screen.getByText(/Quota exhausted/)).toBeTruthy();
    expect(screen.getByText(/Nov 19th, 2026 2:08 PM/)).toBeTruthy();
    expect(screen.getByText(/Runtime switch failed: previous runtime did not stop/)).toBeTruthy();
  });

  it("renders nothing for a task that never failed over", () => {
    const { container } = renderDl({ ...base, serving_account: null, failover: null });
    expect(container.querySelector("dt")).toBeNull();
  });
});

describe("TaskFailover custom account", () => {
  it("shows the configured model and host", () => {
    const { container } = renderDl({
      ...base,
      serving_account: {
        id: "a-c",
        name: "or",
        provider: "custom",
        model: "deepseek/deepseek-chat",
        host: "openrouter.ai",
      },
      failover: null,
    });
    expect(container.textContent).toContain("deepseek/deepseek-chat @ openrouter.ai");
  });
});
