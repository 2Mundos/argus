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
