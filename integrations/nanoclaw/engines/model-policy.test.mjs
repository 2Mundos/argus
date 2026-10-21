import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { loadPolicy, policySnapshot, selectHarnessModel, selectModel } from "./model-policy.mjs";

const policy = {
  harness: { codex: ["luna", "sol"], claude: ["sonnet", "opus"] },
  implementation: { codex: ["sol-6.1"], claude: ["sonnet", "opus"] },
};

test("separate scopes, stable content hash, bounded fallback and pinned override", () => {
  const a = policySnapshot(policy);
  const b = policySnapshot({
    implementation: { claude: ["sonnet", "opus"], codex: ["sol-6.1"] },
    harness: { claude: ["sonnet", "opus"], codex: ["luna", "sol"] },
  });
  assert.equal(a.revision, b.revision);
  assert.equal(selectModel(a, "harness", "codex").configured_model, "luna");
  assert.equal(selectModel(a, "implementation", "codex").configured_model, "sol-6.1");
  const fallback = selectModel(a, "implementation", "claude", 99, "context_limit");
  assert.equal(fallback.requested_model, "sonnet");
  assert.equal(fallback.configured_model, "opus");
  assert.equal(fallback.resolved_model, null);
  assert.equal(fallback.fallback_reason, "context_limit");
  assert.equal(fallback.has_next, false);
  const pin = selectModel(a, "implementation", "claude", 1, null, "custom");
  assert.equal(pin.requested_model, "custom");
  assert.equal(pin.has_next, false);
  assert.notEqual(policySnapshot({ ...policy, harness: {} }).revision, a.revision);
  assert.equal(selectModel(a, "implementation", "agent").configured_model, null);
});

test("rejects malformed policy instead of silently choosing a native default", () => {
  for (const raw of [
    null,
    {},
    [],
    { ...policy, extra: {} },
    { harness: [], implementation: {} },
    ...[[], ["same", "same"], ["--bad"], ["bad\nmodel"], [42], Array(11).fill("x")].map(
      (chain) => ({ harness: { codex: chain }, implementation: {} }),
    ),
    { harness: { typo: ["x"] }, implementation: {} },
  ]) {
    assert.throws(() => policySnapshot(raw));
  }
  assert.throws(() => loadPolicy({ ARGUS_MODEL_POLICY: "not json" }));
  assert.throws(() => selectModel(policySnapshot(policy), "harness", "codex", 2));
  assert.throws(() => selectModel(policySnapshot(policy), "harness", "codex", 0));
});

test("questions do not escalate harness; failures and review rejection do", () => {
  const snapshot = policySnapshot(policy);
  assert.equal(
    selectHarnessModel(snapshot, "claude", { spec: "question answered", failures: 0 })
      .configured_model,
    "sonnet",
  );
  assert.equal(
    selectHarnessModel(snapshot, "claude", { spec: "", failures: 1 }).fallback_reason,
    "previous_task_failure",
  );
  assert.equal(
    selectHarnessModel(snapshot, "claude", { spec: "## Review notes (attempt 1)\nfail" })
      .configured_model,
    "opus",
  );
});

test("shipped defaults reflect operator requirements", () => {
  const snapshot = loadPolicy({});
  assert.equal(selectModel(snapshot, "harness", "codex").configured_model, "gpt-6-luna");
  assert.equal(selectModel(snapshot, "implementation", "codex").configured_model, "gpt-6.1-sol");
  assert.equal(
    selectModel(snapshot, "implementation", "claude", 2, "failed").configured_model,
    "claude-opus-5-5",
  );
});

test("a workspace snapshot wins over process env and tampering is refused", (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "model-policy-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  const file = path.join(dir, "policy.json");
  const snapshot = policySnapshot(policy, { task_id: "t1", attempt: 2 });
  fs.writeFileSync(file, JSON.stringify(snapshot));
  assert.deepEqual(loadPolicy({ ARGUS_MODEL_POLICY: "invalid" }, file), snapshot);
  snapshot.policy.harness.codex = ["tampered"];
  fs.writeFileSync(file, JSON.stringify(snapshot));
  assert.throws(() => loadPolicy({}, file), /revision mismatch/);
});

test("policy file sits between env and defaults", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "policy-"));
  const file = path.join(dir, "model-policy.json");
  assert.equal(loadPolicy({ ARGUS_MODEL_POLICY_PATH: file }).policy.harness.codex[0], "gpt-6-luna");
  fs.writeFileSync(file, JSON.stringify(policy));
  const env = { ARGUS_MODEL_POLICY_PATH: file };
  assert.equal(loadPolicy(env).revision, policySnapshot(policy).revision);
  const other = { harness: { codex: ["env-model"] }, implementation: {} };
  const winner = loadPolicy({ ...env, ARGUS_MODEL_POLICY: JSON.stringify(other) });
  assert.equal(winner.policy.harness.codex[0], "env-model");
  fs.writeFileSync(file, "{broken");
  assert.throws(() => loadPolicy(env));
});

test("revisions match the fixture shared with the manager API", () => {
  const cases = JSON.parse(
    fs.readFileSync(new URL("./model-policy.fixture.json", import.meta.url), "utf8"),
  );
  assert.ok(cases.length >= 3);
  for (const c of cases) {
    const snapshot = policySnapshot(c.policy);
    assert.equal(JSON.stringify(snapshot.policy), c.canonical, c.name);
    assert.equal(snapshot.revision, c.revision, c.name);
  }
  assert.equal(loadPolicy({}).revision, cases[0].revision); // shipped defaults
});
