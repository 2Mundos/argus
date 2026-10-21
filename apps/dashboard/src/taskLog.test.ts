import { describe, expect, it } from "vitest";
import { filterLines, type LogLine, logEvent, MAX_LINES, mergeLines } from "./taskLog";

const line = (seq: number, source: LogLine["source"] = "worker"): LogLine => ({
  seq,
  source,
  at: "2026-10-19T12:00:00Z",
  line: `l${seq}`,
});

describe("logEvent", () => {
  const data = {
    task_id: "t1",
    attempt: 2,
    source: "delegate",
    seq: 5,
    at: "2026-10-19T12:00:00Z",
    lines: ["a", "b"],
  };
  it("expands a batch into numbered lines for its task only", () => {
    const got = logEvent({ kind: "task.log", data }, "t1");
    expect(got?.attempt).toBe(2);
    expect(got?.lines.map((l) => [l.seq, l.line, l.source])).toEqual([
      [5, "a", "delegate"],
      [6, "b", "delegate"],
    ]);
    expect(logEvent({ kind: "task.log", data }, "other")).toBeNull();
    expect(logEvent({ kind: "task.progress", data }, "t1")).toBeNull();
  });
});

describe("mergeLines", () => {
  it("appends in order and drops what backfill already delivered", () => {
    expect(mergeLines([line(1), line(2)], [line(3)]).map((l) => l.seq)).toEqual([1, 2, 3]);
    expect(mergeLines([line(1), line(2)], [line(2), line(3)]).map((l) => l.seq)).toEqual([1, 2, 3]);
    expect(mergeLines([line(3)], [line(1), line(2)]).map((l) => l.seq)).toEqual([1, 2, 3]);
  });
  it("keeps only the newest MAX_LINES", () => {
    const many = Array.from({ length: MAX_LINES + 5 }, (_, i) => line(i + 1));
    const out = mergeLines([], many);
    expect(out).toHaveLength(MAX_LINES);
    expect(out[0]?.seq).toBe(6);
  });
});

describe("filterLines", () => {
  it("filters by source", () => {
    const all = [line(1), line(2, "delegate")];
    expect(filterLines(all, "all")).toHaveLength(2);
    expect(filterLines(all, "delegate").map((l) => l.seq)).toEqual([2]);
    expect(filterLines(all, "worker").map((l) => l.seq)).toEqual([1]);
  });
});
