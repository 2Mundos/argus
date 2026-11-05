import { describe, expect, it } from "vitest";
import { claimedGitIdentity, poolGitIdentity, scopeGitIdentity } from "./argus-git-identity.js";

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
