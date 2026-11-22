import { createHash } from "node:crypto";
import fs from "node:fs";

const engines = new Set(["codex", "claude", "opencode", "agent", "agy", "dsh"]);
// Optional maps, in canonical order after the two required ones: a policy without
// them keeps its revision (task complexity spec).
const OPTIONAL = ["trivial", "hard_harness", "hard_implementation"];
const object = (x) => x !== null && typeof x === "object" && !Array.isArray(x);

export function validatePolicy(raw) {
  const keys = object(raw) ? Object.keys(raw) : [];
  if (
    !object(raw) ||
    !["harness", "implementation"].every((k) => keys.includes(k)) ||
    keys.some((k) => !["harness", "implementation", ...OPTIONAL].includes(k))
  )
    throw new Error(
      "model policy requires harness and implementation maps and allows only optional trivial, hard_harness and hard_implementation ones",
    );
  const policy = {};
  for (const scope of ["harness", "implementation", ...OPTIONAL.filter((k) => keys.includes(k))]) {
    if (!object(raw[scope])) throw new Error(`invalid ${scope} map`);
    policy[scope] = {};
    for (const name of Object.keys(raw[scope]).sort()) {
      const chain = raw[scope][name];
      if (
        !engines.has(name) ||
        !Array.isArray(chain) ||
        chain.length < 1 ||
        chain.length > 10 ||
        chain.some(
          (m) => typeof m !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(m),
        ) ||
        new Set(chain).size !== chain.length
      )
        throw new Error(`invalid model chain: ${scope}.${name}`);
      policy[scope][name] = [...chain];
    }
  }
  return policy;
}

export function policySnapshot(raw, context = null) {
  const policy = validatePolicy(raw);
  const revision = createHash("sha256").update(JSON.stringify(policy)).digest("hex");
  return { policy, revision, context };
}

export function loadPolicy(e = process.env, snapshotFile) {
  if (e.ARGUS_MODEL_POLICY_FILE && snapshotFile && !fs.existsSync(snapshotFile))
    throw new Error("explicit model policy snapshot is missing");
  if (snapshotFile && fs.existsSync(snapshotFile)) {
    const saved = JSON.parse(fs.readFileSync(snapshotFile, "utf8"));
    const snapshot = policySnapshot(saved.policy, saved.context);
    if (snapshot.revision !== saved.revision)
      throw new Error("model policy snapshot revision mismatch");
    return snapshot;
  }
  // Precedence: ARGUS_MODEL_POLICY, then the file the manager API writes
  // (ARGUS_MODEL_POLICY_PATH, i.e. its data/model-policy.json), then the defaults.
  const file = e.ARGUS_MODEL_POLICY_PATH;
  const raw = e.ARGUS_MODEL_POLICY
    ? JSON.parse(e.ARGUS_MODEL_POLICY)
    : file && fs.existsSync(file)
      ? JSON.parse(fs.readFileSync(file, "utf8"))
      : JSON.parse(
          fs.readFileSync(new URL("./model-policy.defaults.json", import.meta.url), "utf8"),
        );
  return policySnapshot(raw);
}

export function selectModel(snapshot, scope, engine, attempt = 1, reason = null, override) {
  if (!Number.isSafeInteger(attempt) || attempt < 1)
    throw new Error("model attempt must be positive");
  if (attempt > 1 && !reason) throw new Error("model fallback requires a reason");
  const chain = snapshot.policy[scope]?.[engine] ?? [];
  const index = override ? 0 : Math.min(attempt - 1, Math.max(0, chain.length - 1));
  const model = override ?? chain[index] ?? null;
  if (override && !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/.test(override))
    throw new Error("invalid model override");
  return {
    scope,
    policy_map: scope,
    policy_engine: engine,
    requested_model: override ?? chain[0] ?? null,
    configured_model: model,
    resolved_model: null,
    policy_revision: snapshot.revision,
    model_attempt: attempt,
    fallback_reason: index > 0 ? reason : null,
    has_next: !override && index + 1 < chain.length,
  };
}

// Reasoning effort levels each role/adapter/provider has an interface for
// (execution-chains spec 3.8). The API also checks per-model rules on save; this
// re-check refuses a level the runtime could not apply instead of dropping it.
const CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"];
export const EFFORTS = {
  "harness:claude:anthropic": CLAUDE_EFFORTS,
  "harness:codex:openai": ["low", "medium", "high", "xhigh"],
  "executor:claude:anthropic": CLAUDE_EFFORTS,
  "executor:codex:openai": ["low", "medium", "high", "xhigh", "max", "ultra"],
  "executor:copilot:copilot": ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
  "executor:agy:google": ["low", "medium", "high", "xhigh", "max"],
};

/** The configured effort of one role of an execution route; null when omitted. */
export function executionEffort(execution, role) {
  const selector = execution?.[role]?.selector;
  const effort = selector?.effort ?? null;
  if (effort === null) return null;
  const key = `${role}:${selector.adapter}:${selector.provider}`;
  if (!EFFORTS[key]?.includes(effort))
    throw new Error(`${role} effort ${effort} is not supported for ${key}`);
  return effort;
}

const PIN_PROVIDERS = { claude: "anthropic", codex: "openai" };

/**
 * The harness model of the attempt. A task pin is fixed while the lease is on the
 * pinned provider (task models spec 3.3): no chain, no fallback. When the engine
 * lease fell back to another provider (`accountProvider` differs from the pin's) the
 * pinned model is dropped for the attempt and the policy default applies.
 */
export function selectHarnessModel(snapshot, engine, task, accountProvider) {
  if (task.execution) {
    const route = task.execution;
    if (
      route.version !== 1 ||
      route.harness.selector.adapter !== engine ||
      route.harness.selector.provider !== accountProvider
    )
      throw new Error("execution harness does not match the selected runtime/account");
    return {
      ...selectModel(snapshot, "harness", engine, 1, null, route.harness.model),
      policy_map: "execution",
      policy_revision: route.revision,
    };
  }
  const pin = task.harness_model;
  if (
    pin?.engine === engine &&
    (accountProvider == null || accountProvider === PIN_PROVIDERS[pin.engine])
  )
    return {
      ...selectModel(snapshot, "harness", engine, 1, null, task.harness_model.model),
      policy_map: "task",
    };
  const reviews = (task.spec.match(/^## Review notes \(attempt /gm) ?? []).length;
  const failures = task.failures ?? 0;
  const scope =
    task.complexity === "hard" && snapshot.policy.hard_harness?.[engine]
      ? "hard_harness"
      : "harness";
  return selectModel(
    snapshot,
    scope,
    engine,
    failures + reviews + 1,
    reviews ? "verification_rejected" : failures ? "previous_task_failure" : null,
  );
}

/**
 * The map delegated selection reads: `hard_implementation` when the pinned task is hard
 * and that map has a chain for this executor; else the optional `trivial` one when the
 * task is trivial or the engine runs on a local account and it has a chain for this
 * executor; else `implementation`.
 */
export function implementationScope(snapshot, executor, context = snapshot.context) {
  if (context?.complexity === "hard" && snapshot.policy.hard_implementation?.[executor])
    return "hard_implementation";
  const light = context?.complexity === "trivial" || context?.account_provider === "local";
  return light && snapshot.policy.trivial?.[executor] ? "trivial" : "implementation";
}
