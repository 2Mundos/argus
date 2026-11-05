import { describe, expect, it } from "vitest";
import { claimedGitIdentity, poolGitIdentity, scopeCommandGuard, scopeGitIdentity } from "./argus-git-identity.js";

const POOL = { ARGUS_GIT_AUTHOR_NAME: "Pool", ARGUS_GIT_AUTHOR_EMAIL: "pool@example.invalid" };
const TASK = { name: "Project Dev", email: "dev@example.invalid" };

describe("task git identity", () => {
  it("prefers the task's identity over the pool's and restores the pool's after", () => {
    const env: NodeJS.ProcessEnv = { ...POOL };
    const pool = poolGitIdentity(env);
    scopeGitIdentity(env, pool, TASK);
    expect(env.ARGUS_GIT_AUTHOR_NAME).toBe("Project Dev");
    expect(env.ARGUS_GIT_AUTHOR_EMAIL).toBe("dev@example.invalid");
    scopeGitIdentity(env, pool, null);
    expect(env).toMatchObject(POOL);
  });

  it("leaves the pool unset, so git refuses, when neither carries one", () => {
    const env: NodeJS.ProcessEnv = {};
    const pool = poolGitIdentity(env);
    scopeGitIdentity(env, pool, TASK);
    scopeGitIdentity(env, pool, null);
    expect("ARGUS_GIT_AUTHOR_NAME" in env).toBe(false);
    expect("ARGUS_GIT_AUTHOR_EMAIL" in env).toBe(false);
  });

  it.each([
    [undefined],
    [null],
    [{}],
    [{ name: "A" }],
    [{ name: " ", email: "a@b" }],
    [{ name: "A", email: 3 }],
  ])("treats %j from the claim as no identity", (raw) => {
    expect(claimedGitIdentity(raw)).toBeNull();
  });

  it("accepts a complete claimed identity", () => {
    expect(claimedGitIdentity({ name: " A ", email: "a@b" })).toEqual({ name: "A", email: "a@b" });
  });
});

describe("task command guard", () => {
  it("turns the guard off for a disabled claim and restores the pool's value after", () => {
    const env: NodeJS.ProcessEnv = {};
    scopeCommandGuard(env, undefined, false);
    expect(env.ARGUS_COMMAND_GUARD).toBe("off");
    scopeCommandGuard(env, undefined, undefined);
    expect("ARGUS_COMMAND_GUARD" in env).toBe(false);
  });

  it.each([[true], [undefined], [null]])("leaves the environment unchanged for %j", (enabled) => {
    const env: NodeJS.ProcessEnv = { ARGUS_COMMAND_GUARD: "on" };
    scopeCommandGuard(env, "on", enabled);
    expect(env.ARGUS_COMMAND_GUARD).toBe("on");
    const bare: NodeJS.ProcessEnv = {};
    scopeCommandGuard(bare, undefined, enabled);
    expect("ARGUS_COMMAND_GUARD" in bare).toBe(false);
  });
});
