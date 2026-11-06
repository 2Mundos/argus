import { describe, expect, it } from "vitest";
import { describeEvent, isTransientEvent } from "./eventLine";

describe("eventLine", () => {
  it("treats task.log and task.changes as transient", () => {
    expect(isTransientEvent({ kind: "task.log" })).toBe(true);
    expect(isTransientEvent({ kind: "task.changes" })).toBe(true);
    expect(isTransientEvent({ kind: "task.claim" })).toBe(false);
  });

  it("describes common event payloads", () => {
    expect(describeEvent({ kind: "task.x", data: { from: "ready", to: "claimed" } })).toBe(
      "ready → claimed",
    );
    expect(describeEvent({ kind: "task.x", data: { message: "hi" } })).toBe("hi");
    expect(describeEvent({ kind: "task.x", data: { reason: "timeout" } })).toBe("timeout");
    expect(describeEvent({ kind: "task.x", data: { to: "failed" } })).toBe("→ failed");
    expect(describeEvent({ kind: "task.x", data: {} })).toBe("");
  });

  it("puts the rule id before the reason for blocked commands", () => {
    const data = { rule: "rm_root", reason: "deletes /" };
    expect(describeEvent({ kind: "task.command_blocked", data })).toBe("rm_root · deletes /");
    expect(describeEvent({ kind: "task.command_blocked", data: { reason: "deletes /" } })).toBe(
      "deletes /",
    );
    expect(describeEvent({ kind: "task.failed", data })).toBe("deletes /");
    const withCommand = { ...data, command: "rm -rf / --token=s3cr3t" };
    expect(describeEvent({ kind: "task.command_blocked", data: withCommand })).toBe(
      "rm_root · deletes /",
    );
  });
});
