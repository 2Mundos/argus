import { describe, expect, it } from "vitest";
import { changesEvent, lineKind, shortSha, snapshotAttempts, splitPatch } from "./taskChanges";

describe("taskChanges", () => {
  it("reads the attempt of a task.changes event for this task only", () => {
    const data = { task_id: "t1", attempt: 2, files: 1 };
    expect(changesEvent({ kind: "task.changes", data }, "t1")).toBe(2);
    expect(changesEvent({ kind: "task.changes", data }, "t2")).toBeNull();
    expect(changesEvent({ kind: "task.log", data }, "t1")).toBeNull();
  });

  it("offers the attempts that keep a snapshot, newest first", () => {
    expect(snapshotAttempts(1)).toEqual([1]);
    expect(snapshotAttempts(5)).toEqual([5, 4, 3]);
  });

  it("splits a patch per file by its b/ path", () => {
    const patch =
      "diff --git a/x.py b/x.py\n@@ -1 +1 @@\n-a\n+b\n" +
      "diff --git a/old.txt b/new.txt\nsimilarity index 100%\n";
    expect(splitPatch(patch).map((p) => p.path)).toEqual(["x.py", "new.txt"]);
    expect(splitPatch(patch)[0]?.text).toBe("diff --git a/x.py b/x.py\n@@ -1 +1 @@\n-a\n+b\n");
    expect(splitPatch("")).toEqual([]);
  });

  it("classifies patch lines", () => {
    expect(
      ["+++ b/x", "--- a/x", "+b", "-a", "@@ -1 +1 @@", "index 1..2", " ctx"].map(lineKind),
    ).toEqual(["meta", "meta", "add", "del", "hunk", "meta", "ctx"]);
    expect(shortSha(null)).toBe("unborn");
    expect(shortSha("0123456789abcdef")).toBe("01234567");
  });
});
