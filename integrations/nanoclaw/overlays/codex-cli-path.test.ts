import { describe, expect, it } from "bun:test";

import { codexCliPath, resolveCodexCliPath } from "./codex-cli-path.js";

const exists =
  (...dirs: string[]) =>
  (dir: string) =>
    dirs.includes(dir);

describe("resolveCodexCliPath", () => {
  it("prepends the engine dir when it exists and is not listed", () => {
    expect(resolveCodexCliPath("/usr/bin:/bin", "/pnpm", exists("/pnpm"))).toBe(
      "/pnpm:/usr/bin:/bin",
    );
  });

  it("leaves PATH unchanged when the engine dir is absent", () => {
    expect(resolveCodexCliPath("/usr/bin:/bin", "/pnpm", exists())).toBe("/usr/bin:/bin");
  });

  it("does not duplicate or reorder an already listed engine dir", () => {
    expect(resolveCodexCliPath("/usr/bin:/pnpm:/bin", "/pnpm", exists("/pnpm"))).toBe(
      "/usr/bin:/pnpm:/bin",
    );
  });

  it("honours an overridden engine dir", () => {
    expect(resolveCodexCliPath("/usr/bin", "/opt/pnpm", exists("/opt/pnpm"))).toBe(
      "/opt/pnpm:/usr/bin",
    );
    expect(resolveCodexCliPath("/usr/bin", "/opt/pnpm", exists("/pnpm"))).toBe("/usr/bin");
  });

  it("uses just the engine dir when there is no base PATH and the dir exists", () => {
    expect(resolveCodexCliPath(undefined, "/pnpm", exists("/pnpm"))).toBe("/pnpm");
  });

  it("never fabricates a PATH when the dir is absent", () => {
    expect(resolveCodexCliPath(undefined, "/pnpm", exists())).toBeUndefined();
  });

  it("only checks the engine dir it was given", () => {
    const seen: string[] = [];
    resolveCodexCliPath("/bin", "/pnpm", (dir) => (seen.push(dir), true));
    expect(seen).toEqual(["/pnpm"]);
  });
});

describe("codexCliPath", () => {
  it("falls back to /pnpm and treats a missing directory as absent", () => {
    expect(codexCliPath("/bin", "/definitely/not/here")).toBe("/bin");
    expect(codexCliPath(undefined, "/definitely/not/here")).toBeUndefined();
  });

  it("checks the real filesystem for a directory", () => {
    expect(codexCliPath("/bin", "/tmp")).toBe("/tmp:/bin");
    expect(codexCliPath("/bin", "/etc/hostname")).toBe("/bin");
  });
});
