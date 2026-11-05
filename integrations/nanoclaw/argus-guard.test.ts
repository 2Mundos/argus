import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { commandGuardBlock, registersCommandGuard } from "./argus-guard.js";

const shipped = readFileSync(
  new URL("./engines/claude-managed-settings.json", import.meta.url),
  "utf8",
);
const path = "/etc/claude-code/managed-settings.json";

describe("command guard presence", () => {
  it("recognizes the shipped managed settings", () => {
    expect(registersCommandGuard(JSON.parse(shipped))).toBe(true);
    expect(commandGuardBlock("claude", path, {}, () => shipped)).toBeNull();
  });

  it.each([
    ["no hooks", "{}"],
    ["other matcher", shipped.replace('"Bash"', '"Read"')],
    ["other command", shipped.replaceAll("command_guard.py", "other.py")],
    ["malformed", "{"],
  ])("blocks env_guard_missing: %s", (_n, body) => {
    expect(commandGuardBlock("claude", path, {}, () => body)?.reason).toBe("env_guard_missing");
  });

  it("blocks when the settings file is unreadable", () => {
    const read = () => {
      throw new Error("ENOENT");
    };
    expect(commandGuardBlock("claude", path, {}, read)?.reason).toBe("env_guard_missing");
  });

  it("does not block when disabled, unguarded or unobservable", () => {
    const empty = () => "{}";
    expect(commandGuardBlock("claude", path, { ARGUS_COMMAND_GUARD: "off" }, empty)).toBeNull();
    expect(commandGuardBlock("claude", path, { ARGUS_COMMAND_GUARD: "0" }, empty)).toBeNull();
    expect(commandGuardBlock("codex", path, {}, empty)).toBeNull();
    expect(commandGuardBlock("claude", undefined, {}, empty)).toBeNull();
  });
});
