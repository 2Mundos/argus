import { describe, expect, it } from "vitest";
import type { ArgusTask } from "./argus.js";
import {
  mergeAuthorizationToken,
  scopeMergeAuthorization,
  taskMayMerge,
} from "./argus-merge-auth.js";

const task = (over: Record<string, unknown> = {}) =>
  ({
    id: "t1",
    attempt: 3,
    deliverable: "pr_merged",
    required_capabilities: ["repository:write"],
    ...over,
  }) as unknown as ArgusTask;

const NAMES = [
  "ARGUS_MERGE_AUTH_KEY",
  "ARGUS_MERGE_AUTH_TASK",
  "ARGUS_MERGE_AUTH_ATTEMPT",
  "ARGUS_MERGE_AUTHORIZED",
];

/** What the channel does at the start of an attempt. */
const scope = (env: NodeJS.ProcessEnv, t: ArgusTask) =>
  scopeMergeAuthorization(env, t, taskMayMerge(t));
const absent = (env: NodeJS.ProcessEnv) => NAMES.every((n) => !(n in env));

describe("merge authorization scope", () => {
  it("issues a key and an attempt-bound token for an auto_merge pr_merged task", () => {
    const env: NodeJS.ProcessEnv = {};
    scope(env, task({ auto_merge: true }));
    expect(env.ARGUS_MERGE_AUTH_TASK).toBe("t1");
    expect(env.ARGUS_MERGE_AUTH_ATTEMPT).toBe("3");
    expect(env.ARGUS_MERGE_AUTH_KEY).toMatch(/^[0-9a-f]{64}$/);
    expect(env.ARGUS_MERGE_AUTHORIZED).toBe(
      mergeAuthorizationToken(env.ARGUS_MERGE_AUTH_KEY as string, "t1", 3),
    );
    expect(env.ARGUS_MERGE_AUTHORIZED).not.toBe(
      mergeAuthorizationToken(env.ARGUS_MERGE_AUTH_KEY as string, "t1", 4),
    );
  });

  it("issues it for allow_merge in an orchestrator task's delivery input", () => {
    const env: NodeJS.ProcessEnv = {};
    scope(env, task({ input: { delivery: { allow_merge: true } } }));
    expect(NAMES.every((n) => env[n])).toBe(true);
  });

  it("leaves the variables absent for unauthorized tasks, and removes a preset bare value", () => {
    for (const t of [
      task(),
      task({ auto_merge: true, deliverable: "pr_opened" }),
      task({ prompt_trust: "worker", input: { delivery: { allow_merge: true } } }),
    ]) {
      const env: NodeJS.ProcessEnv = { ARGUS_MERGE_AUTHORIZED: "1" };
      scope(env, t);
      expect(absent(env)).toBe(true);
    }
  });

  it("never issues to a read-only task", () => {
    const readOnly = task({ auto_merge: true, required_capabilities: ["repository:read"] });
    expect(taskMayMerge(readOnly)).toBe(false);
    expect(taskMayMerge(task({ auto_merge: true, required_capabilities: undefined }))).toBe(false);
    const env: NodeJS.ProcessEnv = {};
    scope(env, readOnly);
    expect(absent(env)).toBe(true);
  });

  it("clears all four when the attempt ends", () => {
    const env: NodeJS.ProcessEnv = {};
    scope(env, task({ auto_merge: true }));
    scopeMergeAuthorization(env, undefined, false);
    expect(absent(env)).toBe(true);
  });
});
