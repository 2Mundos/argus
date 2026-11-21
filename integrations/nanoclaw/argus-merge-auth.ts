/**
 * The merge authorization of an attempt (github repositories spec 3.4).
 *
 * `argus-github-api merge` refuses unless the channel issued an authorization for the
 * running attempt: a random per-attempt key and `HMAC-SHA256(key, "merge:<task>:<attempt>")`
 * as `ARGUS_MERGE_AUTHORIZED`. The verifier in `gitea-api.mjs` recomputes the token from the
 * key, task and attempt variables, so a bare or stale `ARGUS_MERGE_AUTHORIZED` is refused.
 * The variables share the session environment with the worker shell: this refuses bare or
 * stale values; it is not a secrecy boundary.
 */
import { createHmac, randomBytes } from "node:crypto";
import type { ArgusTask } from "./argus.js";

/** True only when the operator's setting or an orchestrator-origin input grants the merge. */
export function taskAuthorizesMerge(task: ArgusTask): boolean {
  // The operator's setting, delivered with the claim, is authenticated metadata.
  if (task.auto_merge === true && task.deliverable === "pr_merged") return true;
  // `input` of a worker-created task is worker-authored: it can never grant authority.
  if (task.prompt_trust === "worker") return false;
  const delivery = task.input?.delivery;
  if (!delivery || typeof delivery !== "object" || Array.isArray(delivery)) return false;
  return (delivery as Record<string, unknown>).allow_merge === true;
}

/** Extract a bare PR number from a PR URL or `#N`/`N` string, else `""`. */
function normalizePrNumber(pr: string | null | undefined): string {
  if (!pr) return "";
  const trimmed = pr.trim().replace(/\/+$/, "");
  const m = trimmed.match(/(?:^#?|\/pulls?\/)(\d{1,9})$/i);
  return m ? m[1] : "";
}

/** Authorized to merge, and allowed to write the repository: a read-only task never merges.
 *  When pre-merge independent review is required, merge stays denied until an approval covers
 *  the exact 40-character head SHA. */
export function taskMayMerge(task: ArgusTask): boolean {
  if (
    task.independent_review_required === true &&
    !/^[0-9a-f]{40}$/i.test((task.independent_review_approved_head ?? "").trim())
  ) {
    return false;
  }
  return (
    taskAuthorizesMerge(task) && task.required_capabilities?.includes("repository:write") === true
  );
}

/** Keep the formula in step with `gitea-api.mjs`. */
export function mergeAuthorizationToken(
  key: string,
  taskId: string,
  attempt: number | string,
  approval?: { pr?: string | null; sha?: string | null },
) {
  const sha = approval?.sha?.trim().toLowerCase() ?? "";
  const pr = sha ? normalizePrNumber(approval?.pr) : "";
  const suffix = sha ? `:review:${pr}:${sha}` : "";
  return createHmac("sha256", key).update(`merge:${taskId}:${attempt}${suffix}`).digest("hex");
}

const VARS = [
  "ARGUS_MERGE_AUTH_KEY",
  "ARGUS_MERGE_AUTH_TASK",
  "ARGUS_MERGE_AUTH_ATTEMPT",
  "ARGUS_MERGE_AUTHORIZED",
  "ARGUS_MERGE_APPROVED_PR",
  "ARGUS_MERGE_APPROVED_SHA",
] as const;

/** Issues the key and token for this attempt; anything else (or `undefined`) removes all six. */
export function scopeMergeAuthorization(
  env: NodeJS.ProcessEnv,
  task:
    | {
        id: string;
        attempt: number;
        independent_review_required?: boolean;
        independent_review_approved_pr?: string | null;
        independent_review_approved_head?: string | null;
      }
    | undefined,
  authorized: boolean,
): void {
  for (const name of VARS) delete env[name];
  if (!authorized || !task) return;
  const approvedSha =
    task.independent_review_required === true
      ? (task.independent_review_approved_head?.trim().toLowerCase() ?? "")
      : "";
  if (task.independent_review_required === true && !/^[0-9a-f]{40}$/.test(approvedSha)) return;
  const approvedPr = approvedSha ? normalizePrNumber(task.independent_review_approved_pr) : "";
  const key = randomBytes(32).toString("hex");
  env.ARGUS_MERGE_AUTH_KEY = key;
  env.ARGUS_MERGE_AUTH_TASK = task.id;
  env.ARGUS_MERGE_AUTH_ATTEMPT = String(task.attempt);
  if (approvedSha) {
    env.ARGUS_MERGE_APPROVED_SHA = approvedSha;
    if (approvedPr) env.ARGUS_MERGE_APPROVED_PR = approvedPr;
  }
  env.ARGUS_MERGE_AUTHORIZED = mergeAuthorizationToken(
    key,
    task.id,
    task.attempt,
    approvedSha ? { pr: approvedPr, sha: approvedSha } : undefined,
  );
}

const FIX_VARS = ["ARGUS_FIX_PR", "ARGUS_FIX_BASE_SHA"] as const;

/**
 * The fix fence of a reviewer attempt (pre-merge independent review spec): a task whose
 * `input.fix` names a PR and the head it reviewed may only publish a fast-forward commit
 * to that PR's own branch. `argus-task deliver` reads these variables and then refuses to
 * rebase, force-push, touch another branch or edit the PR. Anything else removes both; a
 * malformed `fix` sets a PR-less marker so deliver fails closed instead of running open.
 */
export function scopeFixDelivery(
  env: NodeJS.ProcessEnv,
  task: { input?: Record<string, unknown> } | undefined,
): void {
  for (const name of FIX_VARS) delete env[name];
  const fix = task?.input?.fix;
  if (fix === undefined || fix === null) return;
  const valid =
    typeof fix === "object" &&
    !Array.isArray(fix) &&
    (fix as Record<string, unknown>).force_push === false &&
    /^[0-9a-f]{40}$/i.test(String((fix as Record<string, unknown>).base_head_sha ?? "")) &&
    normalizePrNumber(String((fix as Record<string, unknown>).pr ?? "")) !== "";
  if (!valid) {
    env.ARGUS_FIX_PR = "invalid";
    env.ARGUS_FIX_BASE_SHA = "invalid";
    return;
  }
  const f = fix as Record<string, unknown>;
  env.ARGUS_FIX_PR = normalizePrNumber(String(f.pr));
  env.ARGUS_FIX_BASE_SHA = String(f.base_head_sha).toLowerCase();
}
