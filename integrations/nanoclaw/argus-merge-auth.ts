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

/** Authorized to merge, and allowed to write the repository: a read-only task never merges. */
export function taskMayMerge(task: ArgusTask): boolean {
  return (
    taskAuthorizesMerge(task) && task.required_capabilities?.includes("repository:write") === true
  );
}

/** Keep the formula in step with `gitea-api.mjs`. */
export function mergeAuthorizationToken(key: string, taskId: string, attempt: number | string) {
  return createHmac("sha256", key).update(`merge:${taskId}:${attempt}`).digest("hex");
}

const VARS = [
  "ARGUS_MERGE_AUTH_KEY",
  "ARGUS_MERGE_AUTH_TASK",
  "ARGUS_MERGE_AUTH_ATTEMPT",
  "ARGUS_MERGE_AUTHORIZED",
] as const;

/** Issues the key and token for this attempt; anything else (or `undefined`) removes all four. */
export function scopeMergeAuthorization(
  env: NodeJS.ProcessEnv,
  task: { id: string; attempt: number } | undefined,
  authorized: boolean,
): void {
  for (const name of VARS) delete env[name];
  if (!authorized || !task) return;
  const key = randomBytes(32).toString("hex");
  env.ARGUS_MERGE_AUTH_KEY = key;
  env.ARGUS_MERGE_AUTH_TASK = task.id;
  env.ARGUS_MERGE_AUTH_ATTEMPT = String(task.attempt);
  env.ARGUS_MERGE_AUTHORIZED = mergeAuthorizationToken(key, task.id, task.attempt);
}
