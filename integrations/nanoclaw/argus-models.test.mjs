// Integration smoke for the real channel without a NanoClaw checkout or paid calls.
// Node 22.15+ supplies native type stripping and synchronous module hooks.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { registerHooks, stripTypeScriptTypes } from "node:module";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { loadPolicy, selectModel } from "./engines/model-policy.mjs";

registerHooks({
  load(url, context, next) {
    if (url.endsWith(".ts"))
      return {
        shortCircuit: true,
        format: "module",
        source: stripTypeScriptTypes(readFileSync(new URL(url), "utf8"), { mode: "transform" }),
      };
    return next(url, context);
  },
  resolve(specifier, context, next) {
    if (specifier === "../log.js")
      return {
        shortCircuit: true,
        url: "data:text/javascript,export const log = {info(){},warn(){},error(){}}",
      };
    if (specifier === "./channel-registry.js")
      return {
        shortCircuit: true,
        url: "data:text/javascript,export function registerChannelAdapter(){}",
      };
    // Sibling Argus channel modules are the real sources (accounts, custom/local harness, relays).
    const sibling = /^\.\/(argus-[\w-]+)\.js$/.exec(specifier);
    if (sibling)
      return { shortCircuit: true, url: new URL(`./${sibling[1]}.ts`, import.meta.url).href };
    if (specifier === "./model-policy.mjs")
      return {
        shortCircuit: true,
        url: new URL("./engines/model-policy.mjs", import.meta.url).href,
      };
    const stubs = {
      "../db/messaging-groups.js":
        "export async function getMessagingGroupByPlatform(){return {id:'mg'}}; export async function getMessagingGroupAgents(){return [{agent_group_id:'ag'}]}",
      "../db/agent-groups.js":
        "export async function getAgentGroup(){return {id:'ag',folder:'worker'}}",
      "../db/container-configs.js":
        "export async function getContainerConfig(){return globalThis.argusModelTest.config}; export async function updateContainerConfigScalars(id, patch){Object.assign(globalThis.argusModelTest.config, patch)}",
      "../db/sessions.js":
        "export async function getSessionsByAgentGroup(){return [{id:'session1'}]}",
      "../container-runner.js":
        "export function killContainer(id){globalThis.argusModelTest.killed.push(id)}; export function isContainerRunning(){return false}",
    };
    if (stubs[specifier])
      return {
        shortCircuit: true,
        url: "data:text/javascript," + encodeURIComponent(stubs[specifier]),
      };
    return next(specifier, context);
  },
});
const { createArgusAdapter, harnessEffort, modelsFromEnv } = await import("./argus.ts");

async function smoke(t, selectModels) {
  const calls = [];
  const inbound = [];
  const uploads = [];
  let claimed = false;
  const fetch = async (url, init) => {
    const pathname = new URL(url).pathname;
    calls.push(pathname);
    const json = (body) => new Response(JSON.stringify(body), { status: 200 });
    if (pathname.endsWith("/agents/register"))
      return json({ token: "test", agent: { id: "a1" }, heartbeat_interval_s: 60 });
    if (pathname.endsWith("/tasks/claim")) {
      if (!claimed) {
        claimed = true;
        return json({
          id: "task1",
          attempt: 2,
          failures: 1,
          title: "Test",
          spec: "test",
          project: "test",
          acceptance: [],
          command_guard_enabled: false,
          knowledge_refs: [],
        });
      }
      return new Promise((_, reject) =>
        init.signal.addEventListener("abort", () => reject(new Error("aborted"))),
      );
    }
    if (pathname.endsWith("/artifacts")) {
      uploads.push(JSON.parse(new TextDecoder().decode(init.body)));
      return json({ id: "artifact1" });
    }
    return json({});
  };
  const adapter = createArgusAdapter({
    url: "http://test/api",
    bootstrapToken: "test",
    capabilities: [],
    name: "test",
    fetch,
    selectModels,
  });
  t.after(() => adapter.teardown());
  await adapter.setup({
    onInbound: async (...args) => {
      assert.ok(
        calls.some((c) => c.endsWith("/artifacts")),
        "evidence precedes work",
      );
      inbound.push(args);
    },
  });
  const deadline = Date.now() + 1000;
  while (!inbound.length && !calls.some((c) => c.endsWith("/fail"))) {
    assert.ok(Date.now() < deadline, "channel completes setup promptly");
    // check-tests: allow sleep — node:test file; the same poll as argus-test-until.ts
    await new Promise((r) => setTimeout(r, 5));
  }
  return { calls, inbound, uploads, adapter };
}

test("channel records revision and fallback before injecting the actual task", async (t) => {
  const s = await smoke(t, async (task) => {
    assert.equal(task.attempt, 2);
    return selectModel(loadPolicy({}), "harness", "claude", 2, "previous_task_failure");
  });
  assert.equal(s.inbound.length, 1);
  assert.equal(s.uploads[0].configured_model, "claude-opus-5-5");
  assert.equal(s.uploads[0].resolved_model, null);
  assert.equal(s.uploads[0].fallback_reason, "previous_task_failure");
  assert.match(s.uploads[0].policy_revision, /^[a-f0-9]{64}$/);
  await s.adapter.deliver("local", s.inbound[0][1], {
    kind: "chat",
    content: { text: "COMPLETE: done" },
  });
  assert.ok(s.calls.some((c) => c.endsWith("/complete")));
});

test("channel fails closed when model application fails; no task is injected", async (t) => {
  const s = await smoke(t, async () => {
    throw new Error("bad config");
  });
  assert.equal(s.inbound.length, 0);
  assert.equal(s.uploads.length, 0);
  assert.ok(s.calls.some((c) => c.endsWith("/fail")));
});

test("actual harness hook applies config, stops stale containers and pins policy to workspace", async (t) => {
  const cwd = process.cwd();
  const dir = mkdtempSync(path.join(os.tmpdir(), "argus-harness-"));
  const oldPolicy = process.env.ARGUS_MODEL_POLICY;
  process.chdir(dir);
  delete process.env.ARGUS_MODEL_POLICY;
  globalThis.argusModelTest = { config: { provider: "claude", model: "old" }, killed: [] };
  t.after(() => {
    process.chdir(cwd);
    rmSync(dir, { recursive: true, force: true });
    if (oldPolicy === undefined) delete process.env.ARGUS_MODEL_POLICY;
    else process.env.ARGUS_MODEL_POLICY = oldPolicy;
    delete globalThis.argusModelTest;
  });
  const hook = modelsFromEnv();
  process.env.ARGUS_MODEL_POLICY = "invalid after startup";
  const held = {
    engine: { provider: "anthropic", account_id: "a-claude" },
    executor: { provider: "openai", account_id: "a-codex" },
  };
  const selection = await hook({ id: "task1", attempt: 2, failures: 1, spec: "" }, held);
  assert.equal(globalThis.argusModelTest.config.model, "claude-opus-5-5");
  assert.deepEqual(globalThis.argusModelTest.killed, ["session1"]);
  assert.equal(selection.fallback_reason, "previous_task_failure");
  const file = path.join(dir, "groups/worker/argus-model-policy.json");
  const saved = JSON.parse(readFileSync(file, "utf8"));
  assert.equal(saved.revision, selection.policy_revision);
  // The pinned context carries the leased account's engine/provider (global priority
  // chose it) so delegated runs trace to it; no secret or account id rides here.
  assert.deepEqual(saved.context, {
    task_id: "task1",
    attempt: 2,
    engine: "claude",
    account_provider: "anthropic",
    executor_account_provider: "openai",
    engine_account_id: "a-claude",
    executor_account_id: "a-codex",
    preferred_executor: "codex",
    executor_model: null,
    model_switch: null,
    complexity: "standard",
    engine_settings: null,
    executor_settings: null,
  });
  assert.equal(loadPolicy({}, file).policy.implementation.codex[0], "gpt-6.1-sol");
  // Without a separate executor lease the engine's account is the executor account too.
  await hook(
    { id: "task2", attempt: 1, failures: 1, spec: "" },
    { engine: held.engine, executor: null },
  );
  const same = JSON.parse(readFileSync(file, "utf8")).context;
  assert.deepEqual([same.engine_account_id, same.executor_account_id], ["a-claude", "a-claude"]);
  await hook({ id: "task2", attempt: 1, failures: 1, spec: "" }, held);
  assert.equal(globalThis.argusModelTest.killed.length, 1, "unchanged model keeps warm sessions");
  // A trivial task is pinned as such; the leased executor (codex) stays preferred.
  await hook({ id: "task3", attempt: 1, failures: 1, spec: "", complexity: "trivial" }, held);
  const light = JSON.parse(readFileSync(file, "utf8")).context;
  assert.deepEqual([light.complexity, light.preferred_executor], ["trivial", "codex"]);
});

test("route harness effort is applied to the container config and restored after it", async (t) => {
  const cwd = process.cwd();
  const dir = mkdtempSync(path.join(os.tmpdir(), "argus-effort-"));
  process.chdir(dir);
  globalThis.argusModelTest = {
    config: { provider: "codex", model: "gpt-6-luna", effort: "medium" },
    killed: [],
  };
  t.after(() => {
    process.chdir(cwd);
    rmSync(dir, { recursive: true, force: true });
    delete globalThis.argusModelTest;
  });
  const hook = modelsFromEnv();
  const held = { engine: { provider: "openai", account_id: "a-codex" }, executor: null };
  const route = (effort) => ({
    version: 1,
    revision: "r".repeat(64),
    alternative: { mode: "single" },
    harness: {
      model: "gpt-6-luna",
      selector: { adapter: "codex", provider: "openai", ...(effort ? { effort } : {}) },
    },
    executor: null,
  });
  const task = (execution) => ({ id: "t", attempt: 1, spec: "", execution });
  await hook(task(route("xhigh")), held);
  assert.equal(globalThis.argusModelTest.config.effort, "xhigh");
  assert.deepEqual(globalThis.argusModelTest.killed, ["session1"]);
  const pinned = JSON.parse(readFileSync("groups/worker/argus-model-policy.json", "utf8"));
  assert.equal(pinned.context.execution.harness.selector.effort, "xhigh");
  // Same effort again: no restart. Omitted effort and a legacy task restore the operator's.
  await hook(task(route("xhigh")), held);
  assert.equal(globalThis.argusModelTest.killed.length, 1);
  await hook(task(route(null)), held);
  assert.equal(globalThis.argusModelTest.config.effort, "medium");
  await hook(task(route("low")), held);
  await hook({ id: "t", attempt: 1, spec: "" }, held);
  assert.equal(globalThis.argusModelTest.config.effort, "medium");
  // A level the runtime cannot apply fails the attempt; nothing is changed.
  await assert.rejects(hook(task(route("ultra")), held), /harness effort ultra is not supported/);
  assert.equal(globalThis.argusModelTest.config.effort, "medium");
  // An operator edit made while a route effort is applied wins on restore.
  const groupDir = path.join(dir, "groups/worker");
  assert.equal(await harnessEffort(groupDir, "medium", route("high")), "high");
  assert.equal(await harnessEffort(groupDir, "low", route(null)), "low");
  assert.equal(await harnessEffort(groupDir, null, route("high")), "high");
  assert.equal(await harnessEffort(groupDir, "high", task(null).execution), null);
});

test("channel installer is idempotent and includes source and built policy assets", (t) => {
  const source = process.env.ARGUS_TEST_NANOCLAW_SOURCE;
  if (!source)
    return t.skip(
      "requires the pinned NanoClaw checkout; pass ARGUS_TEST_NANOCLAW_SOURCE from provision.sh check",
    );
  const target = mkdtempSync(path.join(os.tmpdir(), "argus-install-"));
  t.after(() => rmSync(target, { recursive: true, force: true }));
  const archive = spawnSync("git", ["-C", source, "archive", "HEAD"], {
    maxBuffer: 32 * 1024 * 1024,
  });
  assert.equal(archive.status, 0, archive.stderr.toString());
  const unpack = spawnSync("tar", ["-xf", "-", "-C", target], { input: archive.stdout });
  assert.equal(unpack.status, 0, unpack.stderr.toString());
  for (let i = 0; i < 2; i++) {
    const result = spawnSync("bash", [new URL("./install.sh", import.meta.url).pathname, target], {
      encoding: "utf8",
    });
    assert.equal(result.status, 0, result.stderr);
  }
  for (const dir of ["src/channels", "dist/channels"]) {
    const saved = JSON.parse(
      readFileSync(path.join(target, dir, "model-policy.defaults.json"), "utf8"),
    );
    assert.equal(saved.harness.codex[0], "gpt-6-luna");
    assert.ok(
      readFileSync(path.join(target, dir, "model-policy.mjs"), "utf8").includes("selectModel"),
    );
  }
  const runner = path.join(target, "container/agent-runner/src");
  const claude = readFileSync(path.join(runner, "providers/claude.ts"), "utf8");
  for (const hook of [
    "claudeRateLimitError(m.result, rateLimited)",
    "recordHarnessUsage('claude'",
    "recordHarnessLog(message)",
  ])
    assert.equal(claude.split(hook).length - 1, 1, `${hook} patched once after two installs`);
  const poll = readFileSync(path.join(runner, "poll-loop.ts"), "utf8");
  assert.equal(poll.split("claudeRateLimitFailure(error)").length - 1, 1, "poll-loop hooked once");
  assert.equal(
    readFileSync(path.join(target, "src/channels/index.ts"), "utf8")
      .split("import './argus.js'")
      .slice(1).length,
    1,
  );
});
