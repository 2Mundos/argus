import { describe, expect, it } from "vitest";
import type { LogLine } from "../taskLog";
import { taskTerminalVisibleTabs } from "./taskTerminalTabs";

const line = (source: LogLine["source"]): LogLine => ({
  seq: 1,
  source,
  at: "2026-01-01T00:00:00Z",
  line: "x",
});

describe("taskTerminalVisibleTabs", () => {
  it("shows only All while the log is still loading", () => {
    expect(taskTerminalVisibleTabs({ ready: false, lines: [] }, true, false)).toEqual(["all"]);
  });

  it("drops empty output tabs once the log is ready", () => {
    expect(taskTerminalVisibleTabs({ ready: true, lines: [line("delegate")] }, true, true)).toEqual(
      ["delegate", "changes"],
    );
  });

  it("shows All only when both harness and delegate have lines", () => {
    expect(
      taskTerminalVisibleTabs(
        { ready: true, lines: [line("worker"), { ...line("delegate"), seq: 2 }] },
        false,
        false,
      ),
    ).toEqual(["all", "worker", "delegate"]);
  });

  it("omits Changes without a snapshot", () => {
    expect(taskTerminalVisibleTabs({ ready: true, lines: [line("worker")] }, true, false)).toEqual([
      "worker",
    ]);
  });
});
