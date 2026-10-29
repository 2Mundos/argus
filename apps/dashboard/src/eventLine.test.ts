import { describe, expect, it } from "vitest";
import { describeEvent, isTransientEvent } from "./eventLine";

describe("eventLine", () => {
  it("treats task.log and task.changes as transient", () => {
    expect(isTransientEvent({ kind: "task.log" })).toBe(true);
    expect(isTransientEvent({ kind: "task.changes" })).toBe(true);
    expect(isTransientEvent({ kind: "task.claim" })).toBe(false);
  });

  it("describes common event payloads", () => {
    expect(describeEvent({ data: { from: "ready", to: "claimed" } })).toBe("ready → claimed");
    expect(describeEvent({ data: { message: "hi" } })).toBe("hi");
    expect(describeEvent({ data: { reason: "timeout" } })).toBe("timeout");
    expect(describeEvent({ data: { to: "failed" } })).toBe("→ failed");
    expect(describeEvent({ data: {} })).toBe("");
  });
});
