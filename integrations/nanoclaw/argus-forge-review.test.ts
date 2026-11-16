import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { forgeReviewLines, forgeReviewPolicy, scopeForgeReview } from "./argus-forge-review.js";

const CASES = JSON.parse(
  readFileSync(new URL("./fixtures/merge-decision/cases.json", import.meta.url), "utf8"),
);

describe("forge review policy", () => {
  it("matches the decision cases the API shows on the task read", () => {
    for (const c of CASES.review) {
      expect(
        forgeReviewPolicy({ forge_agentic_review_required: c.forge_agentic_review_required }),
      ).toBe(c.policy);
    }
  });

  it("is off only for an explicit false; an absent or odd value is required", () => {
    expect(forgeReviewPolicy({ forge_agentic_review_required: false })).toBe("off");
    for (const value of [true, undefined, null, "false", 0]) {
      expect(forgeReviewPolicy({ forge_agentic_review_required: value })).toBe("required");
    }
    expect(forgeReviewPolicy({})).toBe("required");
  });

  it("sets the env for the attempt and removes it after", () => {
    const env: NodeJS.ProcessEnv = { ARGUS_FORGE_REVIEW: "off" };
    scopeForgeReview(env, { forge_agentic_review_required: true });
    expect(env.ARGUS_FORGE_REVIEW).toBe("required");
    scopeForgeReview(env, { forge_agentic_review_required: false });
    expect(env.ARGUS_FORGE_REVIEW).toBe("off");
    scopeForgeReview(env, {});
    expect(env.ARGUS_FORGE_REVIEW).toBe("required");
    scopeForgeReview(env, undefined);
    expect("ARGUS_FORGE_REVIEW" in env).toBe(false);
  });

  it("tells the model when review applies instead of to wait blindly", () => {
    const off = forgeReviewLines({ forge_agentic_review_required: false }).join("\n");
    expect(off).toContain("not required");
    expect(off).toContain("Do not wait for one");
    expect(off).toContain("changes-requested");
    expect(off).toContain("--skip-review does not remove them");
    expect(off).toContain("never ask for a forge comment");
    const required = forgeReviewLines({ forge_agentic_review_required: true }).join("\n");
    expect(required).toContain("required by this project's policy");
    expect(required).toContain("argus-task wait review");
    expect(required).toContain("--skip-review does not apply");
    expect(forgeReviewLines({}).join("\n")).toBe(required);
  });
});
