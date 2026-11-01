// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Activity } from "./Activity";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("Activity quiet claims", () => {
  const now = Date.parse("2026-10-29T12:00:00.000Z");

  it("shows the stall sentence for a quiet claimed task", () => {
    vi.useFakeTimers({ now: new Date(now) });
    const quietAt = new Date(now - 1500 * 1000).toISOString();
    render(
      <Activity
        task={{
          id: "t1",
          state: "claimed",
          attempt: 1,
          progress_message: null,
          progress_percent: null,
          progress_at: null,
          progress_kind: null,
          progress_data: null,
          lease_expires_at: new Date(now + 3600 * 1000).toISOString(),
          quiet_at: quietAt,
          last_signal_at: null,
          last_signal_source: null,
          stall_cause: "workspace_missing",
        }}
        events={[]}
      />,
    );
    expect(screen.getByRole("status").textContent?.replace(/\s+/g, " ")).toBe(
      "Quiet for 25m; likely cause: workspace missing on the host.",
    );
  });

  it("includes last signal when the read carries it", () => {
    vi.useFakeTimers({ now: new Date(now) });
    const quietAt = new Date(now - 1500 * 1000).toISOString();
    const progressAt = new Date(now - 180 * 1000).toISOString();
    render(
      <Activity
        task={{
          id: "t1",
          state: "claimed",
          attempt: 1,
          progress_message: "working",
          progress_percent: 10,
          progress_at: progressAt,
          progress_kind: null,
          progress_data: null,
          lease_expires_at: new Date(now + 3600 * 1000).toISOString(),
          quiet_at: quietAt,
          last_signal_at: progressAt,
          last_signal_source: "progress",
          stall_cause: "workspace_missing",
        }}
        events={[]}
      />,
    );
    const text = screen.getByRole("status").textContent ?? "";
    expect(text).toMatch(/Quiet for 25m/);
    expect(text).toMatch(/last signal: progress at/);
    expect(text).toMatch(/likely cause: workspace missing on the host\./);
  });

  it("renders no stall sentence for a busy claimed task", () => {
    vi.useFakeTimers({ now: new Date(now) });
    render(
      <Activity
        task={{
          id: "t1",
          state: "claimed",
          attempt: 1,
          progress_message: null,
          progress_percent: null,
          progress_at: null,
          progress_kind: null,
          progress_data: null,
          lease_expires_at: new Date(now + 3600 * 1000).toISOString(),
          quiet_at: null,
          last_signal_at: new Date(now - 60 * 1000).toISOString(),
          last_signal_source: "claim",
          stall_cause: null,
        }}
        events={[]}
      />,
    );
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByText(/Quiet for/)).toBeNull();
  });
});

describe("Activity finalization", () => {
  const base = {
    id: "t1",
    state: "failed" as const,
    attempt: 1,
    progress_message: null,
    progress_percent: null,
    progress_at: null,
    progress_kind: null,
    progress_data: null,
    lease_expires_at: null,
  };
  const fin = (step: string, checkpoint: object | null, execution_s: number | null = 125) => ({
    attempt: 1,
    step,
    claim_ended_at: "2026-10-29T12:00:00.000Z",
    claim_end_action: "fail",
    execution_s,
    checkpoint,
  });
  const cp = (state: string, over: object = {}) => ({
    state,
    started_at: "2026-10-29T12:00:01.000Z",
    ended_at: null,
    duration_s: null,
    detail: null,
    ...over,
  });

  it.each([
    ["finished", null, "Agent finished (took 2m)."],
    ["finalizing_checkpoint", cp("running"), "Agent finished (took 2m). Saving checkpoint…"],
    [
      "finished",
      cp("failed", { detail: "env_checkpoint_limit", duration_s: 4 }),
      "Agent finished (took 2m). Checkpoint failed: env_checkpoint_limit (took 4s).",
    ],
    [
      "finished",
      cp("saved", { detail: "checkpoint:x:y", duration_s: 3 }),
      "Agent finished (took 2m). Checkpoint saved (took 3s).",
    ],
  ])("%s: %j", (step, checkpoint, text) => {
    // biome-ignore lint/suspicious/noExplicitAny: a hand-built read fixture
    const task: any = { ...base, finalization: fin(step, checkpoint) };
    render(<Activity task={task} events={[]} />);
    expect(screen.getByText(text)).toBeTruthy();
  });

  it("gives no duration when the execution end is not the agent's", () => {
    // biome-ignore lint/suspicious/noExplicitAny: a hand-built read fixture
    const task: any = { ...base, finalization: fin("finished", null, null) };
    render(<Activity task={task} events={[]} />);
    expect(screen.getByText("Agent finished.")).toBeTruthy();
  });
});
