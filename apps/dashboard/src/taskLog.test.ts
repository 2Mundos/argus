import { describe, expect, it } from "vitest";
import {
  filterLines,
  type LogLine,
  logDownload,
  logEvent,
  MAX_LINES,
  mergeLines,
  visibleLines,
} from "./taskLog";

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

describe("visibleLines and logDownload", () => {
  const all = [line(1), line(2, "delegate"), line(3), line(4, "delegate")];

  it("applies the pause cutoff, then the source filter", () => {
    expect(visibleLines(all, "all", null).map((l) => l.seq)).toEqual([1, 2, 3, 4]);
    expect(visibleLines(all, "all", 3).map((l) => l.seq)).toEqual([1, 2, 3]);
    expect(visibleLines(all, "delegate", 3).map((l) => l.seq)).toEqual([2]);
    expect(visibleLines(all, "worker", null).map((l) => l.seq)).toEqual([1, 3]);
  });

  it("writes only the given rows, with the header and the delegate prefix", () => {
    const rows = visibleLines(all, "all", 3);
    expect(logDownload("t1", 2, "all", rows, 3)).toEqual({
      filename: "argus-task-t1-attempt-2-all.txt",
      text: [
        "# Argus task log: loaded excerpt, not a full transcript",
        "# task: t1",
        "# attempt: 2",
        "# source: all",
        "# cutoff: paused after seq 3",
        "# lines: 3",
        "",
        "l1",
        "[delegate] l2",
        "l3",
        "",
      ].join("\n"),
    });
    expect(logDownload("t1", 2, "all", all, null).text).toContain("# cutoff: live\n");
  });

  it("sanitizes and caps the task id in the filename", () => {
    const { filename } = logDownload(`a/b ${"x".repeat(80)}`, 1, "worker", [], null);
    expect(filename).toBe(`argus-task-a-b-${"x".repeat(60)}-attempt-1-worker.txt`);
  });
});
