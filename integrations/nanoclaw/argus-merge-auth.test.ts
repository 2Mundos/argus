import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { ArgusTask } from "./argus.js";
import {
  mergeAuthorizationToken,
  scopeFixDelivery,
  scopeMergeAuthorization,
  taskAuthorizesMerge,
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

  it("denies merge when independent review is required without approval and binds PR/SHA once approved", () => {
    const sha = "a".repeat(40);
    const unapproved = task({ auto_merge: true, independent_review_required: true });
    expect(taskAuthorizesMerge(unapproved)).toBe(true);
    expect(taskMayMerge(unapproved)).toBe(false);
    const env: NodeJS.ProcessEnv = {};
    scope(env, unapproved);
    expect(absent(env)).toBe(true);

    const approved = task({
      auto_merge: true,
      independent_review_required: true,
      independent_review_approved_pr: "https://github.com/acme/widget/pull/450",
      independent_review_approved_head: sha,
    });
    expect(taskMayMerge(approved)).toBe(true);
    scope(env, approved);
    expect(env.ARGUS_MERGE_APPROVED_PR).toBe("450");
    expect(env.ARGUS_MERGE_APPROVED_SHA).toBe(sha);
    expect(env.ARGUS_MERGE_AUTHORIZED).toBe(
      mergeAuthorizationToken(env.ARGUS_MERGE_AUTH_KEY as string, "t1", 3, {
        pr: "450",
        sha,
      }),
    );
  });
});

// Shared with apps/api/tests/test_task_readiness.py: the API shows this decision on the task read.
const CASES = JSON.parse(
  readFileSync(new URL("./fixtures/merge-decision/cases.json", import.meta.url), "utf8"),
);

describe("merge decision cases shared with the API", () => {
  for (const c of CASES.merge) {
    it(c.name, () => {
      const t = task(c.task);
      expect(taskAuthorizesMerge(t)).toBe(c.authorized);
      expect(taskMayMerge(t)).toBe(c.may_merge);
    });
  }
});

describe("scopeFixDelivery", () => {
  const sha = "a".repeat(40);
  const fix = (f: unknown) => ({ input: { fix: f } });

  it("exports the PR and reviewed head for a valid fix scope, and clears them otherwise", () => {
    const env: NodeJS.ProcessEnv = {};
    scopeFixDelivery(
      env,
      fix({
        pr: "https://github.com/o/r/pull/461",
        base_head_sha: sha.toUpperCase(),
        force_push: false,
      }),
    );
    expect(env.ARGUS_FIX_PR).toBe("461");
    expect(env.ARGUS_FIX_BASE_SHA).toBe(sha);
    scopeFixDelivery(env, { input: {} });
    expect(env.ARGUS_FIX_PR).toBeUndefined();
    expect(env.ARGUS_FIX_BASE_SHA).toBeUndefined();
    scopeFixDelivery(env, undefined);
    expect("ARGUS_FIX_PR" in env).toBe(false);
  });

  it.each([
    fix({ pr: 5, base_head_sha: sha, force_push: true }),
    fix({ pr: 5, base_head_sha: "abc", force_push: false }),
    fix({ pr: "x", base_head_sha: sha, force_push: false }),
    fix("nope"),
  ])("fails closed on a malformed scope", (task) => {
    const env: NodeJS.ProcessEnv = {};
    scopeFixDelivery(env, task);
    expect(env.ARGUS_FIX_PR).toBe("invalid");
    expect(env.ARGUS_FIX_BASE_SHA).toBe("invalid");
  });
});
