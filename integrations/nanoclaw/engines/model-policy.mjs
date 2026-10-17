import { createHash } from "node:crypto";
import fs from "node:fs";

const engines = new Set(["codex", "claude", "opencode", "agent"]);
const object = (x) => x !== null && typeof x === "object" && !Array.isArray(x);

export function validatePolicy(raw) {
  if (!object(raw) || Object.keys(raw).sort().join(",") !== "harness,implementation")
    throw new Error("model policy requires only harness and implementation maps");
  const policy = {};
  for (const scope of ["harness", "implementation"]) {
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

export function selectHarnessModel(snapshot, engine, task) {
  const reviews = (task.spec.match(/^## Review notes \(attempt /gm) ?? []).length;
  const failures = task.failures ?? 0;
  return selectModel(
    snapshot,
    "harness",
    engine,
    failures + reviews + 1,
    reviews ? "verification_rejected" : failures ? "previous_task_failure" : null,
  );
}
