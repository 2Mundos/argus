/**
 * The forge agentic-review policy of a claim (settings spec 3.12).
 *
 * Argus resolves `review.forge_agentic` (project over global) and sends it in the claim as
 * `forge_agentic_review_required`. The channel puts it in its own environment for the attempt
 * (`ARGUS_FORGE_REVIEW=required|off`), which the session spawn forwards to `argus-task`, and
 * words the task prompt from it. Nothing here reads task text. Only an explicit `false` means
 * "off": a claim without the field (an older API) is `required`, so an unknown policy never
 * drops a gate. It is distinct from `review.independent`, Argus's own `code_review` child.
 */
export type ForgeReviewPolicy = "required" | "off";

const ENV = "ARGUS_FORGE_REVIEW";

export function forgeReviewPolicy(task: {
  forge_agentic_review_required?: unknown;
}): ForgeReviewPolicy {
  return task.forge_agentic_review_required === false ? "off" : "required";
}

/** Sets the policy for the attempt; `undefined` (between tasks) removes it, so a standalone helper sees none. */
export function scopeForgeReview(env: NodeJS.ProcessEnv, task: object | undefined): void {
  if (task) env[ENV] = forgeReviewPolicy(task);
  else delete env[ENV];
}

/** Trusted prompt lines for a repository task that may open or ready a PR. */
export function forgeReviewLines(task: { forge_agentic_review_required?: unknown }): string[] {
  const standing =
    "Whatever the policy, a reviewer's standing changes-requested review, or P1–P3 findings in an agentic review that exists for the current head, still block ready and merge; --skip-review does not remove them and is not a workaround.";
  const internal =
    "Any review by a different model that Argus requires runs inside Argus after COMPLETE; never ask for a forge comment for it.";
  if (forgeReviewPolicy(task) === "off")
    return [
      "Forge agentic review: not required by this project's policy. Do not wait for one, request one, or treat its absence as a blocker; argus-task pr-ready does not require one. Required CI on the exact head, mergeability and branch protection still apply.",
      standing,
      internal,
    ];
  return [
    "Forge agentic review: required by this project's policy (or the policy was not delivered, which counts as required). Wait for an agentic review of the exact head with argus-task wait review, resolve its P1–P3 findings, and do not call the PR ready or merge it until review-findings shows a finding count for that head. --skip-review does not apply.",
    standing,
    internal,
  ];
}
