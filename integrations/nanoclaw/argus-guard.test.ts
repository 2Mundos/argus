import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import {
  commandGuardBlock,
  DEFAULT_MANAGED_SETTINGS_PATH,
  readImageFile,
  registersCommandGuard,
  sessionImageCommandGuardBlock,
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

// The NanoClaw host image has no /etc/claude-code: the hook lives in the agent image only,
// so the worker must read the session's image, never its own filesystem.
describe("worker command guard check against the session image", () => {
  const image = async () => "argus-nanoclaw-agent:test";

  it("passes when the session image registers the hook, whatever the host holds", async () => {
    const seen: string[][] = [];
    const read = async (ref: string, p: string) => {
      seen.push([ref, p]);
      return shipped;
    };
    expect(await sessionImageCommandGuardBlock("claude", image, read, {})).toBeNull();
    expect(seen).toEqual([["argus-nanoclaw-agent:test", DEFAULT_MANAGED_SETTINGS_PATH]]);
  });

  it.each([
    ["no hooks", async () => "{}"],
    ["no command_guard", async () => shipped.replaceAll("command_guard.py", "other.py")],
    [
      "no file in the image",
      async () => {
        throw new Error("cat: /etc/claude-code/managed-settings.json: No such file or directory");
      },
    ],
  ])("blocks env_guard_missing naming the image: %s", async (_n, read) => {
    const block = await sessionImageCommandGuardBlock("claude", image, read, {});
    expect(block?.reason).toBe("env_guard_missing");
    expect(block?.error).toContain(
      `${DEFAULT_MANAGED_SETTINGS_PATH} in image argus-nanoclaw-agent:test`,
    );
  });

  it("blocks when the session image cannot be resolved", async () => {
    const none = async () => {
      throw new Error("no agent group is wired to the argus channel");
    };
    const block = await sessionImageCommandGuardBlock("claude", none, async () => shipped, {});
    expect(block?.reason).toBe("env_guard_missing");
  });

  it("does not look at the image when disabled or unguarded", async () => {
    const read = async () => {
      throw new Error("must not run");
    };
    for (const [engine, env] of [
      ["claude", { ARGUS_COMMAND_GUARD: "off" }],
      ["claude", { ARGUS_COMMAND_GUARD: "0" }],
      ["codex", {}],
    ] as const) {
      expect(await sessionImageCommandGuardBlock(engine, image, read, env)).toBeNull();
    }
  });
});

describe("reading a file from an image", () => {
  const dir = mkdtempSync(join(tmpdir(), "argus-guard-bin-"));
  afterAll(() => rmSync(dir, { recursive: true, force: true }));
  const bin = (name: string, body: string) => {
    const p = join(dir, name);
    writeFileSync(p, `#!/bin/sh\n${body}\n`);
    chmodSync(p, 0o755);
    return p;
  };

  it("runs a throwaway container that never pulls and has no network", async () => {
    const echo = bin("echo-args", `printf '%s\\n' "$@"`);
    const out = await readImageFile("agent:1", DEFAULT_MANAGED_SETTINGS_PATH, echo);
    expect(out.trim().split("\n")).toEqual([
      "run",
      "--rm",
      "--pull=never",
      "--network=none",
      "--entrypoint",
      "cat",
      "agent:1",
      DEFAULT_MANAGED_SETTINGS_PATH,
    ]);
  });

  it("rejects when the runtime fails, and never passes an option as the image", async () => {
    const fail = bin("fail", "echo 'No such image' >&2; exit 125");
    await expect(readImageFile("agent:1", DEFAULT_MANAGED_SETTINGS_PATH, fail)).rejects.toThrow();
    const echo = bin("echo-ok", "echo ok");
    await expect(readImageFile("--privileged", "/x", echo)).rejects.toThrow(/not an image/);
    await expect(readImageFile("", "/x", echo)).rejects.toThrow(/not an image/);
  });
});
