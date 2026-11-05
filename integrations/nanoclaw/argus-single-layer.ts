/**
 * Single-layer coding run experiment (docs/specs/single-layer-experiment.spec.md, P7).
 *
 * Off by default. With ARGUS_SINGLE_LAYER=claude and an assignment file, a task the
 * file assigns to `single_layer` runs with the Claude harness as the coding model:
 * implementation-tier model, no argus-delegate hop. Every other path is the default
 * one. A task that is assigned but ineligible falls back to the default path with a
 * recorded reason; it is never emulated through the delegate.
 */
import { readFileSync } from "node:fs";
import {
  implementationScope,
  type ModelSelection,
  type PolicySnapshot,
  selectHarnessModel,
  selectModel,
} from "./model-policy.mjs";

export type Arm = "single_layer" | "delegated";

export interface SingleLayerConfig {
  /** The only supported harness engine. */
  engine: "claude";
  /** JSON written by scripts/single_layer_ab.py assign: {experiment, engine, arms}. */
  assignmentFile: string;
}

/** What the claim ran as, and why it is not what was assigned (null: as assigned). */
export interface ArmDecision {
  experiment: string;
  assigned: Arm;
  arm: Arm;
  fallback: string | null;
}

const EXPERIMENT_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/;

/** The flag: on only for `claude` with an assignment file; anything else is off, with why. */
export function parseSingleLayerEnv(env: NodeJS.ProcessEnv): {
  config: SingleLayerConfig | null;
  refused: string | null;
} {
  const flag = (env.ARGUS_SINGLE_LAYER ?? "").trim().toLowerCase();
  if (!flag || flag === "0" || flag === "off") return { config: null, refused: null };
  if (flag !== "claude")
    return { config: null, refused: `unsupported_engine: ${flag} (only claude is supported)` };
  const file = (env.ARGUS_SINGLE_LAYER_ASSIGNMENT ?? "").trim();
  if (!file) return { config: null, refused: "no_assignment: ARGUS_SINGLE_LAYER_ASSIGNMENT unset" };
  return { config: { engine: "claude", assignmentFile: file }, refused: null };
}

/** The arm the assignment gives this task, or null when the task is not in the experiment. */
export function assignedArm(raw: unknown, taskId: string): { experiment: string; arm: Arm } | null {
  const doc = raw as { experiment?: unknown; engine?: unknown; arms?: unknown } | null;
  if (!doc || typeof doc !== "object") throw new Error("assignment is not an object");
  if (typeof doc.experiment !== "string" || !EXPERIMENT_ID.test(doc.experiment))
    throw new Error("assignment has no valid experiment id");
  if (doc.engine !== "claude") throw new Error("assignment engine must be claude");
  if (!doc.arms || typeof doc.arms !== "object") throw new Error("assignment has no arms");
  const arm = (doc.arms as Record<string, unknown>)[taskId];
  if (arm === undefined) return null;
  if (arm !== "single_layer" && arm !== "delegated")
    throw new Error(`assignment arm for ${taskId} is invalid`);
  return { experiment: doc.experiment, arm };
}

export function readAssignment(file: string): unknown {
  return JSON.parse(readFileSync(file, "utf8"));
}

/** Why an assigned task cannot run single-layer on this claim, or null when it can. */
export function ineligibility(
  task: {
    harness_model?: unknown;
    executor_model?: unknown;
    difficulty_executors?: string[] | null;
  },
  held: { engine: string | null; provider: string | null },
): string | null {
  if (!held.provider) return "no_engine_lease";
  if (held.engine !== "claude" || held.provider !== "anthropic") return "engine_not_claude";
  if (task.harness_model) return "harness_model_pinned";
  if (task.executor_model) return "executor_model_pinned";
  if (Array.isArray(task.difficulty_executors) && task.difficulty_executors.length === 0)
    return "no_delegated_executor"; // already direct: both arms would be the same run
  return null;
}

/** The decision for one claim; null when the experiment does not touch the task. */
export function decideArm(
  assigned: { experiment: string; arm: Arm } | null,
  reason: string | null,
): ArmDecision | null {
  if (!assigned) return null;
  const fallback = assigned.arm === "single_layer" ? reason : null;
  return {
    experiment: assigned.experiment,
    assigned: assigned.arm,
    arm: fallback ? "delegated" : assigned.arm,
    fallback,
  };
}

/** The arm marker on the harness selection (model_selections.explanation, no secret). */
export function armMarker(d: ArmDecision): string {
  const base = `single-layer experiment ${d.experiment}: arm=${d.arm}`;
  return d.fallback ? `${base} assigned=${d.assigned} fallback=${d.fallback}` : base;
}

export const SINGLE_LAYER_SCOPES = ["implementation", "hard_implementation", "trivial"];

/**
 * The harness model. Single-layer takes the implementation tier the delegate would
 * have used for a Claude executor; it falls back to the default harness selection when
 * the policy has no such chain (the adapter then records `no_implementation_chain`).
 */
export function harnessSelection(
  snapshot: PolicySnapshot,
  engine: string,
  task: Parameters<typeof selectHarnessModel>[2] & { complexity?: string | null },
  accountProvider: string | null | undefined,
  singleLayer = false,
): ModelSelection {
  if (singleLayer && engine === "claude") {
    const scope = implementationScope(snapshot, "claude", {
      complexity: task.complexity ?? "standard",
      account_provider: accountProvider ?? null,
    });
    const chains = snapshot.policy as unknown as Record<string, Record<string, string[]>>;
    if (chains[scope]?.claude?.length) {
      // The same fallback position the harness chain would take on a retry.
      const reviews = (task.spec.match(/^## Review notes \(attempt /gm) ?? []).length;
      const failures = task.failures ?? 0;
      return selectModel(
        snapshot,
        scope,
        "claude",
        failures + reviews + 1,
        reviews ? "verification_rejected" : failures ? "previous_task_failure" : null,
      );
    }
  }
  return selectHarnessModel(snapshot, engine, task, accountProvider);
}

/** Prompt lines for the single-layer arm, in place of the delegation block. */
export const SINGLE_LAYER_LINES = [
  "Single-layer run (experiment): you are the coding model for this task. Do the investigation,",
  "implementation, gates, QA and review yourself in the task checkout. Do not call",
  "/usr/local/bin/argus-delegate and do not start another coding CLI. Direct Git and shell commands are fine.",
  "When you open the pull request, name its URL in a progress message or in COMPLETE.",
];

/**
 * Pull requests of `repository` named in `text`, as pr_opened data (the URL without query).
 * Host, owner and repository compare case-insensitively, as the forges and Argus treat
 * them: the claim's URL is lowercase, while the forge prints the owner's own casing.
 */
export function pullRequestsOf(
  repository: string | undefined,
  text: string,
): Array<{ number: number; url: string }> {
  if (!repository) return [];
  const base = repository.replace(/(\.git)?\/*$/, "").toLowerCase();
  const found = new Map<string, { number: number; url: string }>();
  for (const m of text.matchAll(/https?:\/\/[^\s"'<>)\]\\]+/g)) {
    const bare = m[0]
      .replace(/[?#].*$/, "")
      .replace(/[.,;:]+$/, "")
      .replace(/\/+$/, "");
    const pr = /^(.*)\/pulls?\/(\d+)$/.exec(bare);
    if (!pr || pr[1]!.toLowerCase() !== base) continue;
    const number = Number(pr[2]);
    if (Number.isSafeInteger(number) && number >= 1) found.set(bare, { number, url: bare });
  }
  return [...found.values()];
}
