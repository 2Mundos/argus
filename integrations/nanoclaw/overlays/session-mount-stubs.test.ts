import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { ensureSessionMountStubs } from "./session-mount-stubs.js";

const temps: string[] = [];

function tempSessionDir(): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "session-mount-stubs-"));
  temps.push(dir);
  return dir;
}

afterEach(() => {
  for (const dir of temps.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

describe("ensureSessionMountStubs", () => {
  it("creates agent and group as directories mode 0o755 owned by the host uid", () => {
    const sessionDir = tempSessionDir();
    ensureSessionMountStubs(sessionDir);

    for (const name of ["agent", "group"] as const) {
      const stub = path.join(sessionDir, name);
      const st = fs.statSync(stub);
      expect(st.isDirectory()).toBe(true);
      expect(st.mode & 0o777).toBe(0o755);
      if (typeof process.getuid === "function" && process.getuid() !== 0) {
        expect(st.uid).toBe(process.getuid());
        expect(st.uid).not.toBe(0);
      }
    }
  });

  it("is idempotent and does not chmod an existing directory", () => {
    const sessionDir = tempSessionDir();
    const agent = path.join(sessionDir, "agent");
    fs.mkdirSync(agent, { mode: 0o700 });
    fs.chmodSync(agent, 0o700);
    expect(fs.statSync(agent).mode & 0o777).toBe(0o700);

    ensureSessionMountStubs(sessionDir);
    ensureSessionMountStubs(sessionDir);

    expect(fs.statSync(agent).mode & 0o777).toBe(0o700);
    expect(fs.statSync(path.join(sessionDir, "group")).isDirectory()).toBe(true);
    expect(fs.statSync(path.join(sessionDir, "group")).mode & 0o777).toBe(0o755);
  });

  it("leaves an existing non-directory untouched", () => {
    const sessionDir = tempSessionDir();
    const group = path.join(sessionDir, "group");
    fs.writeFileSync(group, "not-a-dir");
    const before = fs.statSync(group);

    ensureSessionMountStubs(sessionDir);

    const after = fs.statSync(group);
    expect(after.isFile()).toBe(true);
    expect(after.mode).toBe(before.mode);
    expect(fs.readFileSync(group, "utf8")).toBe("not-a-dir");
    expect(fs.statSync(path.join(sessionDir, "agent")).isDirectory()).toBe(true);
  });

  it("leaves an existing symlink alone instead of following it", () => {
    const sessionDir = tempSessionDir();
    const target = path.join(sessionDir, "missing-target");
    fs.symlinkSync(target, path.join(sessionDir, "agent"));

    ensureSessionMountStubs(sessionDir);

    expect(fs.lstatSync(path.join(sessionDir, "agent")).isSymbolicLink()).toBe(true);
    expect(fs.existsSync(target)).toBe(false);
  });

  it("does not touch unrelated sibling entries", () => {
    const sessionDir = tempSessionDir();
    const sibling = path.join(sessionDir, "outbox");
    fs.mkdirSync(sibling, { mode: 0o700 });
    fs.chmodSync(sibling, 0o700);
    fs.writeFileSync(path.join(sibling, "note"), "keep");

    ensureSessionMountStubs(sessionDir);

    expect(fs.statSync(sibling).mode & 0o777).toBe(0o700);
    expect(fs.readFileSync(path.join(sibling, "note"), "utf8")).toBe("keep");
    expect(fs.readdirSync(sessionDir).sort()).toEqual(["agent", "group", "outbox"]);
  });
});
