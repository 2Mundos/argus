import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import {
  commandGuardBlock,
  DEFAULT_MANAGED_SETTINGS_PATH,
  registersCommandGuard,
  workerCommandGuardBlock,
} from "./argus-guard.js";

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
    expect(
      commandGuardBlock("claude", undefined, { ARGUS_COMMAND_GUARD: "off" }, empty),
    ).toBeNull();
  });

  it("checks the image default when no path is configured", () => {
    for (const unset of [undefined, "", "  "]) {
      const seen: string[] = [];
      const read = (p: string) => {
        seen.push(p);
        return "{}";
      };
      expect(commandGuardBlock("claude", unset, {}, read)?.reason).toBe("env_guard_missing");
      expect(seen).toEqual([DEFAULT_MANAGED_SETTINGS_PATH]);
    }
    const readDefault = (p: string) => {
      expect(p).toBe(DEFAULT_MANAGED_SETTINGS_PATH);
      return shipped;
    };
    expect(commandGuardBlock("claude", undefined, {}, readDefault)).toBeNull();
  });
});

describe("worker command guard check against real files", () => {
  const dir = mkdtempSync(join(tmpdir(), "argus-guard-"));
  afterAll(() => rmSync(dir, { recursive: true, force: true }));
  const file = (name: string, body: string) => {
    const p = join(dir, name);
    writeFileSync(p, body);
    return p;
  };

  it("blocks a guarded engine without the hook when the env var is unset", () => {
    const read = () => {
      throw new Error("ENOENT");
    };
    expect(workerCommandGuardBlock("claude", undefined, {}, read)?.reason).toBe(
      "env_guard_missing",
    );
  });

  it.each([
    ["empty settings", "{}"],
    ["no command_guard", shipped.replaceAll("command_guard.py", "other.py")],
  ])("blocks when ARGUS_CLAUDE_MANAGED_SETTINGS points at a file with %s", (name, body) => {
    const env = { ARGUS_CLAUDE_MANAGED_SETTINGS: file(`${name}.json`, body) };
    expect(workerCommandGuardBlock("claude", undefined, env)?.reason).toBe("env_guard_missing");
  });

  it("blocks when the env var points at a missing file", () => {
    const env = { ARGUS_CLAUDE_MANAGED_SETTINGS: join(dir, "absent.json") };
    expect(workerCommandGuardBlock("claude", undefined, env)?.reason).toBe("env_guard_missing");
  });

  it("passes when the env var or the channel path names a file with the hook", () => {
    const ok = file("shipped.json", shipped);
    expect(
      workerCommandGuardBlock("claude", undefined, { ARGUS_CLAUDE_MANAGED_SETTINGS: ok }),
    ).toBeNull();
    expect(workerCommandGuardBlock("claude", ok, {})).toBeNull();
  });

  it("leaves other engines and the off switch alone", () => {
    expect(workerCommandGuardBlock("codex", undefined, {})).toBeNull();
    expect(workerCommandGuardBlock("claude", undefined, { ARGUS_COMMAND_GUARD: "off" })).toBeNull();
  });
});
