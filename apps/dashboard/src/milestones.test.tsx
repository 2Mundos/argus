// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { NO_SIGNAL_MINUTES, noSignalMinutes, parseNoSignalMinutes, TaskSignal } from "./milestones";

afterEach(cleanup);

const NOW = Date.parse("2026-10-19T12:00:00Z");
const ago = (min: number) => new Date(NOW - min * 60_000).toISOString();

const base = {
  state: "claimed" as const,
  progress_message: "Delegating implementation",
  progress_percent: null,
  progress_at: ago(1),
  progress_kind: null,
  progress_data: null,
};

describe("TaskSignal", () => {
  it("shows the milestone label, short sha and a PR link", () => {
    const { rerender } = render(
      <TaskSignal
        now={NOW}
        task={{
          ...base,
          progress_kind: "commit_created",
          progress_data: { branch: "feat/x", sha: "0123456789abcdef", at: ago(2) },
        }}
      />,
    );
    expect(screen.getByText("Commit created")).toBeTruthy();
    expect(screen.getByText("0123456")).toBeTruthy();
    expect(screen.getByText(/2 min ago|2m ago|ago/)).toBeTruthy();

    rerender(
      <TaskSignal
        now={NOW}
        task={{
          ...base,
          progress_kind: "pr_opened",
          progress_data: { number: 30, url: "https://git.example/o/r/pulls/30", at: ago(1) },
        }}
      />,
    );
    const link = screen.getByRole("link", { name: "#30" });
    expect(link.getAttribute("href")).toBe("https://git.example/o/r/pulls/30");
    expect(screen.getByText("PR opened")).toBeTruthy();
  });

  it("does not link a non-http url", () => {
    render(
      <TaskSignal
        now={NOW}
        task={{
          ...base,
          progress_kind: "pr_opened",
          progress_data: { number: 3, url: "javascript:alert(1)", at: ago(1) },
        }}
      />,
    );
    expect(screen.queryByRole("link")).toBeNull();
  });

  it("flags a quiet claimed task and marks the last note stale", () => {
    render(<TaskSignal now={NOW} task={{ ...base, progress_at: ago(35) }} />);
    expect(screen.getByRole("status").textContent).toBe("No signal for 35 min");
    expect(screen.getByText(/Last note: Delegating/)).toBeTruthy();
  });

  it("renders a legacy task (no kind) as a plain note", () => {
    render(<TaskSignal now={NOW} task={base} />);
    expect(screen.getByText("Delegating implementation")).toBeTruthy();
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByText("Commit created")).toBeNull();
  });
});

describe("no-signal threshold", () => {
  it("defaults to 10 minutes and ignores unclaimed tasks", () => {
    expect(NO_SIGNAL_MINUTES).toBe(10);
    expect(noSignalMinutes(base, NOW)).toBeNull();
    expect(noSignalMinutes({ ...base, progress_at: ago(11) }, NOW)).toBe(11);
    expect(noSignalMinutes({ state: "ready", progress_at: ago(99) }, NOW)).toBeNull();
    expect(noSignalMinutes({ ...base, progress_at: ago(5) }, NOW, 3)).toBe(5);
  });

  it("counts silence from the latest of progress_at and output_at", () => {
    const quiet = { ...base, progress_at: ago(35) };
    expect(noSignalMinutes(quiet, NOW)).toBe(35);
    expect(noSignalMinutes({ ...quiet, output_at: ago(2) }, NOW)).toBeNull();
    expect(noSignalMinutes({ ...quiet, output_at: ago(20) }, NOW)).toBe(20);
    // An older log line never hides a newer note.
    expect(noSignalMinutes({ ...base, progress_at: ago(1), output_at: ago(40) }, NOW)).toBeNull();
    // Output alone (no note yet) still counts; neither: no warning.
    expect(noSignalMinutes({ ...base, progress_at: null, output_at: ago(30) }, NOW)).toBe(30);
    expect(noSignalMinutes({ ...base, progress_at: null, output_at: null }, NOW)).toBeNull();
    expect(noSignalMinutes({ ...quiet, output_at: ago(8) }, NOW, 3)).toBe(8);
  });

  it("hides the badge while the task is producing output", () => {
    const { rerender } = render(
      <TaskSignal now={NOW} task={{ ...base, progress_at: ago(35), output_at: ago(1) }} />,
    );
    expect(screen.queryByRole("status")).toBeNull();
    rerender(<TaskSignal now={NOW} task={{ ...base, progress_at: ago(35), output_at: ago(30) }} />);
    expect(screen.getByRole("status").textContent).toBe("No signal for 30 min");
  });

  it("parses the Vite env override and falls back on junk", () => {
    expect(parseNoSignalMinutes("25")).toBe(25);
    expect(parseNoSignalMinutes(undefined)).toBe(10);
    expect(parseNoSignalMinutes("")).toBe(10);
    expect(parseNoSignalMinutes("abc")).toBe(10);
    expect(parseNoSignalMinutes("-3")).toBe(10);
  });
});
