import { describe, expect, it } from "vitest";
import { bulkBody, confirmLabel } from "./archive";

const base = { states: ["failed" as const], project: "", olderThanDays: "" };

describe("bulkBody", () => {
  it("sends only the criteria that were set", () => {
    expect(bulkBody(base, true)).toEqual({ states: ["failed"], dry_run: true });
    expect(bulkBody({ ...base, project: "web", olderThanDays: "30" }, false)).toEqual({
      states: ["failed"],
      project: "web",
      older_than_days: 30,
      dry_run: false,
    });
  });

  it("selects nothing without a state or with an invalid age", () => {
    expect(bulkBody({ ...base, states: [] }, true)).toBeNull();
    expect(bulkBody({ ...base, olderThanDays: "-1" }, true)).toBeNull();
    expect(bulkBody({ ...base, olderThanDays: "1.5" }, true)).toBeNull();
  });

  it("never sends a state that is not terminal", () => {
    expect(bulkBody({ ...base, states: ["failed", "claimed", "ready"] }, true)?.states).toEqual([
      "failed",
    ]);
  });
});

describe("confirmLabel", () => {
  it("names the count to be archived", () => {
    expect(confirmLabel(undefined)).toBe("Counting…");
    expect(confirmLabel(0)).toBe("Nothing to archive");
    expect(confirmLabel(1)).toBe("Archive 1 task");
    expect(confirmLabel(42)).toBe("Archive 42 tasks");
  });
});
