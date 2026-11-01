import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  authorizedCwd,
  authStatus,
  buildCommand,
  COPILOT_SENTINEL_TOKEN,
  CUSTOM_KEY_ENV,
  copilotAuthRejected,
  cursorConfigDirAllowsCliWrites,
  executorJsonFailure,
  localHarnessRefusal,
  materializePrivateCursorConfig,
  parseAccountSettings,
  pinnedModel,
  prepareExecutorEnv,
  READ_ONLY_PROMPT_PREFIX,
  withCliToolsPath,
} from "./argus-delegate.mjs";
import { policySnapshot } from "./model-policy.mjs";

const DELEGATE = path.join(path.dirname(fileURLToPath(import.meta.url)), "argus-delegate.mjs");

/** A sandbox: a workspace root, a fake HOME, and fake executables first on PATH. */
function sandbox(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "delegate-"));
  t.after(() => {
    const relax = (p) => {
      try {
        const st = fs.statSync(p);
        fs.chmodSync(p, st.isDirectory() ? 0o755 : 0o644);
        if (st.isDirectory()) for (const name of fs.readdirSync(p)) relax(path.join(p, name));
      } catch {
        /* ignore */
      }
    };
    relax(dir);
    fs.rmSync(dir, { recursive: true, force: true });
  });
  const work = path.join(dir, "work");
  const home = path.join(dir, "home");
  const bin = path.join(dir, "bin");
  for (const d of [work, home, bin]) fs.mkdirSync(d);
  const fake = (name, script) => {
    fs.writeFileSync(path.join(bin, name), `#!/bin/sh\n${script}\n`, { mode: 0o755 });
  };
  const env = {
    PATH: `${bin}:${process.env.PATH}`,
    HOME: home,
    ARGUS_DELEGATE_ROOTS: work,
    ARGUS_ENGINE: "codex",
  };
  return { dir, work, home, bin, fake, env };
}

function run(args, { env, input = "do the thing" }) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [DELEGATE, ...args], { env });
    let out = "";
    child.stdout.on("data", (c) => (out += c));
    child.stdin.end(input);
    child.on("close", (code) => {
      const last = out.trim().split("\n").pop();
      resolve({ code, report: JSON.parse(last) });
    });
  });
}

test("a successful run reports engine and executor identity and keeps artifacts", async (t) => {
  const s = sandbox(t);
  s.fake(
    "claude",
    'if [ "$1" = "--version" ]; then echo "9.9.9 (Claude Code)"; exit 0; fi\ncat >/dev/null; echo \'{"result":"hi"}\'',
  );
  const { code, report } = await run(["claude", "--cwd", s.work, "--timeout", "20"], {
    env: { ...s.env, ANTHROPIC_API_KEY: "placeholder" },
  });
  assert.equal(code, 0);
  assert.equal(report.engine, "codex");
  assert.equal(report.executor, "claude");
  assert.equal(report.version, "9.9.9 (Claude Code)");
  assert.equal(report.timed_out, false);
  assert.equal(fs.readFileSync(report.artifacts[0], "utf8").trim(), '{"result":"hi"}');
  assert.ok(report.artifacts[0].startsWith(fs.realpathSync(s.work)));
});

test("the pinned task context names the leased engine and account provider (global priority)", async (t) => {
  const s = sandbox(t);
  s.fake(
    "claude",
    'if [ "$1" = "--version" ]; then echo "9.9.9 (Claude Code)"; exit 0; fi\ncat >/dev/null; echo \'{"result":"hi"}\'',
  );
  const policy = JSON.parse(
    fs.readFileSync(new URL("./model-policy.defaults.json", import.meta.url), "utf8"),
  );
  const snapshot = policySnapshot(policy, {
    task_id: "t1",
    attempt: 1,
    engine: "claude",
    account_provider: "anthropic",
    executor_account_provider: "anthropic",
    preferred_executor: "claude",
  });
  const file = path.join(s.dir, "argus-model-policy.json");
  fs.writeFileSync(file, JSON.stringify(snapshot));
  const env = { ...s.env, ANTHROPIC_API_KEY: "placeholder", ARGUS_MODEL_POLICY_FILE: file };
  delete env.ARGUS_ENGINE;
  const { code, report } = await run(["claude", "--cwd", s.work, "--timeout", "20"], { env });
  assert.equal(code, 0);
  assert.equal(report.engine, "claude"); // not "unknown", not a stale Codex default
  assert.equal(report.account_provider, "anthropic");
  // The caller's executor is the one the leased accounts prefer; no silent switch.
  assert.deepEqual([report.preferred_executor, report.matches_preferred], ["claude", true]);
  assert.equal(report.task.account_provider, "anthropic");
  assert.equal(JSON.stringify(report).includes("placeholder"), false);
});

test("a task's executor pin sets the model for that executor only, with no chain", async (t) => {
  const s = sandbox(t);
  s.fake(
    "claude",
    'if [ "$1" = "--version" ]; then echo "9.9.9 (Claude Code)"; exit 0; fi\ncat >/dev/null; echo \'{"result":"hi"}\'',
  );
  const policy = JSON.parse(
    fs.readFileSync(new URL("./model-policy.defaults.json", import.meta.url), "utf8"),
  );
  const snapshot = policySnapshot(policy, {
    task_id: "t1",
    attempt: 1,
    engine: "claude",
    preferred_executor: "claude",
    executor_model: { executor: "claude", model: "claude-opus-5-5" },
  });
  const file = path.join(s.dir, "argus-model-policy.json");
  fs.writeFileSync(file, JSON.stringify(snapshot));
  const env = { ...s.env, ANTHROPIC_API_KEY: "placeholder", ARGUS_MODEL_POLICY_FILE: file };
  const { code, report } = await run(["claude", "--cwd", s.work, "--timeout", "20"], { env });
  assert.equal(code, 0);
  assert.deepEqual([report.model, report.model_source], ["claude-opus-5-5", "task"]);
  assert.equal(pinnedModel(snapshot, "codex"), undefined);
});

test("the report carries the harness model switch of the attempt", async (t) => {
  const s = sandbox(t);
  s.fake(
    "claude",
    'if [ "$1" = "--version" ]; then echo "9.9.9 (Claude Code)"; exit 0; fi\ncat >/dev/null; echo \'{"result":"hi"}\'',
  );
  const policy = JSON.parse(
    fs.readFileSync(new URL("./model-policy.defaults.json", import.meta.url), "utf8"),
  );
  const model_switch = {
    from: { provider: "anthropic", model: "claude-opus-5-5", account_id: null },
    to: { provider: "openai", model: null, account_id: "acc-codex", account_name: "codex" },
    reason: "exhausted",
    attempt: 1,
  };
  const snapshot = policySnapshot(policy, {
    task_id: "t1",
    attempt: 1,
    engine: "codex",
    preferred_executor: "claude",
    model_switch,
  });
  const file = path.join(s.dir, "argus-model-policy.json");
  fs.writeFileSync(file, JSON.stringify(snapshot));
  const env = { ...s.env, ANTHROPIC_API_KEY: "placeholder", ARGUS_MODEL_POLICY_FILE: file };
  const { code, report } = await run(["claude", "--cwd", s.work, "--timeout", "20"], { env });
  assert.equal(code, 0);
  assert.deepEqual(report.model_switch, model_switch);
});

test("missing credentials block with 78 and run nothing, with no fallback", async (t) => {
  const s = sandbox(t);
  const marker = path.join(s.dir, "ran");
  for (const name of ["agent", "opencode", "codex", "claude"]) {
    s.fake(name, `if [ "$1" = "--version" ]; then echo v; exit 0; fi\ntouch ${marker}`);
  }
  for (const executor of ["agent", "opencode", "codex", "claude"]) {
    const { code, report } = await run([executor, "--cwd", s.work], { env: s.env });
    assert.equal(code, 78, executor);
    assert.equal(report.blocked, "auth_missing");
    assert.equal(report.executor, executor);
    assert.ok(report.need);
  }
  assert.equal(fs.existsSync(marker), false, "no executor may have been started");
});

test("an empty auth stub is not a credential", (t) => {
  const s = sandbox(t);
  fs.mkdirSync(path.join(s.home, ".codex"));
  fs.writeFileSync(path.join(s.home, ".codex", "auth.json"), "");
  assert.equal(authStatus("codex", {}, s.home).ok, false);
  fs.writeFileSync(path.join(s.home, ".codex", "auth.json"), '{"auth_mode":"chatgpt","tokens":{}}');
  assert.equal(authStatus("codex", {}, s.home).ok, true);
});

test("the credential check never needs the secret itself", () => {
  assert.equal(authStatus("agent", { CURSOR_API_KEY: "x" }, "/nonexistent").ok, false);
  assert.equal(authStatus("agent", {}, "/nonexistent").ok, false);
  assert.equal(
    authStatus("opencode", { ARGUS_OPENCODE_ATTACH: "http://127.0.0.1:4096" }, "/nonexistent").ok,
    true,
  );
});

test("a cwd outside the allowed roots is refused with 77", async (t) => {
  const s = sandbox(t);
  s.fake("claude", "exit 0");
  const { code, report } = await run(["claude", "--cwd", s.home], {
    env: { ...s.env, ANTHROPIC_API_KEY: "x" },
  });
  assert.equal(code, 77);
  assert.equal(report.blocked, "cwd_not_allowed");
});

test("a symlink out of the root does not smuggle the cwd in", (t) => {
  const s = sandbox(t);
  fs.symlinkSync(s.home, path.join(s.work, "escape"));
  assert.equal(authorizedCwd(path.join(s.work, "escape"), [s.work]), null);
  assert.equal(authorizedCwd(path.join(s.work, "../work"), [s.work]), fs.realpathSync(s.work));
  assert.equal(authorizedCwd(s.work + "-evil", [s.work]), null);
});

test("timeout kills the whole process tree and exits 124", async (t) => {
  const s = sandbox(t);
  const pidFile = path.join(s.dir, "grandchild.pid");
  s.fake(
    "codex",
    `if [ "$1" = "--version" ]; then echo codex-cli 0; exit 0; fi\nsleep 300 &\necho $! > ${pidFile}\nwait`,
  );
  const started = Date.now();
  const { code, report } = await run(["codex", "--cwd", s.work, "--timeout", "1"], {
    env: { ...s.env, OPENAI_API_KEY: "x" },
  });
  assert.equal(code, 124);
  assert.equal(report.timed_out, true);
  assert.ok(Date.now() - started < 15000);
  const pid = Number(fs.readFileSync(pidFile, "utf8"));
  assert.throws(() => process.kill(pid, 0), { code: "ESRCH" }, "the grandchild must be dead");
});

test("an executor that fails is reported as a failure, not hidden", async (t) => {
  const s = sandbox(t);
  s.fake("opencode", 'if [ "$1" = "--version" ]; then echo 1.0; exit 0; fi\necho boom >&2; exit 3');
  const { code, report } = await run(["opencode", "--cwd", s.work], {
    env: { ...s.env, ARGUS_OPENCODE_ATTACH: "http://127.0.0.1:1" },
  });
  assert.equal(code, 1);
  assert.equal(report.exit_code, 3);
  assert.match(fs.readFileSync(report.artifacts[1], "utf8"), /boom/);
});

test("not installed is distinct from not authenticated", async (t) => {
  const s = sandbox(t);
  const authDir = path.join(s.home, ".config", "cursor");
  fs.mkdirSync(authDir, { recursive: true });
  fs.writeFileSync(
    path.join(authDir, "auth.json"),
    '{"accessToken":"sentinel","refreshToken":"stub"}',
  );
  const { code, report } = await run(["agent", "--cwd", s.work], {
    env: { ...s.env, PATH: s.bin, HOME: s.home },
  });
  assert.equal(code, 127);
  assert.equal(report.blocked, "not_installed");
});

test("prepareExecutorEnv writes cli-config beside auth when the tree is writable", (t) => {
  const s = sandbox(t);
  const dir = path.join(s.dir, "runtime");
  const authDir = path.join(s.home, ".config", "cursor");
  fs.mkdirSync(authDir, { recursive: true });
  fs.writeFileSync(
    path.join(authDir, "auth.json"),
    '{"accessToken":"sentinel","refreshToken":"stub"}',
  );
  const out = prepareExecutorEnv("agent", { ...s.env, HOME: s.home }, dir);
  assert.equal(out.CURSOR_CONFIG_DIR, authDir);
  const cfg = JSON.parse(fs.readFileSync(path.join(authDir, "cli-config.json"), "utf8"));
  assert.equal(cfg.network.useHttp1ForAgent, true);
  assert.equal(prepareExecutorEnv("claude", out, dir), out);
});

function lockCursorDirReadOnly(cursor) {
  fs.chmodSync(path.join(cursor, "auth.json"), 0o444);
  if (fs.existsSync(path.join(cursor, "cli-config.json"))) {
    fs.chmodSync(path.join(cursor, "cli-config.json"), 0o444);
  }
  fs.chmodSync(cursor, 0o555);
}

test("prepareExecutorEnv does not write read-only injected cli-config that already enables HTTP/1.1", (t) => {
  const s = sandbox(t);
  const probe = path.join(s.dir, "probe");
  const cursor = path.join(probe, "cursor");
  fs.mkdirSync(cursor, { recursive: true });
  fs.writeFileSync(
    path.join(cursor, "auth.json"),
    '{"accessToken":"sentinel","refreshToken":"stub"}',
  );
  fs.writeFileSync(
    path.join(cursor, "cli-config.json"),
    '{"version":1,"network":{"useHttp1ForAgent":true}}',
  );
  lockCursorDirReadOnly(cursor);
  const before = fs.readFileSync(path.join(cursor, "cli-config.json"), "utf8");
  const runtime = path.join(s.dir, "runtime");
  const out = prepareExecutorEnv(
    "agent",
    { HOME: s.home, XDG_CONFIG_HOME: probe, CURSOR_CONFIG_DIR: cursor },
    runtime,
  );
  assert.equal(out.CURSOR_CONFIG_DIR, path.join(runtime, "cursor-config"));
  assert.equal(fs.readFileSync(path.join(cursor, "cli-config.json"), "utf8"), before);
  assert.ok(fs.existsSync(path.join(out.CURSOR_CONFIG_DIR, "auth.json")));
  assert.ok(cursorConfigDirAllowsCliWrites(out.CURSOR_CONFIG_DIR));
});

test("prepareExecutorEnv uses a private writable dir when injected cli-config is read-only and lacks HTTP/1.1", (t) => {
  const s = sandbox(t);
  const probe = path.join(s.dir, "probe");
  const cursor = path.join(probe, "cursor");
  fs.mkdirSync(cursor, { recursive: true });
  fs.writeFileSync(
    path.join(cursor, "auth.json"),
    '{"accessToken":"sentinel","refreshToken":"stub"}',
  );
  fs.writeFileSync(path.join(cursor, "cli-config.json"), '{"version":1,"network":{}}');
  const runtime = path.join(s.dir, "runtime");
  lockCursorDirReadOnly(cursor);
  const out = prepareExecutorEnv(
    "agent",
    { HOME: s.home, XDG_CONFIG_HOME: probe, CURSOR_CONFIG_DIR: cursor },
    runtime,
  );
  assert.equal(out.CURSOR_CONFIG_DIR, path.join(runtime, "cursor-config"));
  assert.equal(
    JSON.parse(fs.readFileSync(path.join(out.CURSOR_CONFIG_DIR, "cli-config.json"), "utf8")).network
      .useHttp1ForAgent,
    true,
  );
});

test("materializePrivateCursorConfig copies sentinel auth without mutating the stub mount", (t) => {
  const s = sandbox(t);
  const stub = path.join(s.dir, "stub");
  fs.mkdirSync(stub, { recursive: true });
  fs.writeFileSync(
    path.join(stub, "auth.json"),
    '{"accessToken":"sentinel","refreshToken":"stub"}',
  );
  fs.writeFileSync(
    path.join(stub, "cli-config.json"),
    '{"version":1,"network":{"useHttp1ForAgent":true}}',
  );
  lockCursorDirReadOnly(stub);
  const privateDir = materializePrivateCursorConfig(stub, path.join(s.dir, "runtime"));
  assert.ok(cursorConfigDirAllowsCliWrites(privateDir));
  assert.equal(
    JSON.parse(fs.readFileSync(path.join(privateDir, "auth.json"), "utf8")).accessToken,
    "sentinel",
  );
});

test("commands are non-interactive, scoped to the cwd, and read-only on request", () => {
  const codex = buildCommand("codex", {
    cwd: "/workspace/x",
    readOnly: true,
    lastMessageFile: "/o",
  });
  assert.deepEqual(codex.args.slice(0, 4), [
    "exec",
    "--json",
    "--skip-git-repo-check",
    "--ephemeral",
  ]);
  assert.ok(codex.args.join(" ").includes("-C /workspace/x -s read-only"));
  const write = buildCommand("codex", { cwd: "/w", readOnly: false, lastMessageFile: "/o" });
  assert.ok(write.args.join(" ").includes("-s workspace-write"));
  assert.ok(buildCommand("claude", { cwd: "/w", readOnly: true }).args.includes("plan"));
  const claudeWrite = buildCommand("claude", { cwd: "/w", readOnly: false });
  // The live log needs the event stream; the last line is still the result object.
  assert.equal(claudeWrite.args[claudeWrite.args.indexOf("--output-format") + 1], "stream-json");
  assert.ok(claudeWrite.args.includes("--verbose"));
  assert.ok(claudeWrite.args.includes("acceptEdits"));
  assert.ok(claudeWrite.args.includes("--allowed-tools"));
  assert.ok(claudeWrite.args.join(" ").includes("Bash"));
  const agentWrite = buildCommand("agent", { cwd: "/w", readOnly: false });
  assert.equal(agentWrite.args[agentWrite.args.indexOf("--output-format") + 1], "stream-json");
  assert.equal(agentWrite.args[agentWrite.args.indexOf("--background-shell-timeout") + 1], "120");
  const bounded = buildCommand("agent", { cwd: "/w" }, { ARGUS_AGENT_BG_SHELL_TIMEOUT_S: "30" });
  assert.equal(bounded.args[bounded.args.indexOf("--background-shell-timeout") + 1], "30");
  const junk = buildCommand("agent", { cwd: "/w" }, { ARGUS_AGENT_BG_SHELL_TIMEOUT_S: "-5" });
  assert.equal(junk.args[junk.args.indexOf("--background-shell-timeout") + 1], "120");
  assert.ok(agentWrite.args.includes("--trust"));
  assert.ok(agentWrite.args.includes("--force"));
  assert.equal(agentWrite.args.indexOf("--trust"), agentWrite.args.indexOf("--workspace") + 2);
  assert.equal(agentWrite.args.includes("--mode"), false);
  const agentRo = buildCommand("agent", { cwd: "/w", readOnly: true });
  assert.ok(agentRo.args.includes("--trust"));
  assert.equal(agentRo.readOnlyEnforcement, "prompt_only");
  for (const forbidden of ["--mode", "--plan", "--force", "plan", "ask"]) {
    assert.equal(
      agentRo.args.includes(forbidden),
      false,
      `read-only agent must not pass ${forbidden}`,
    );
  }
  for (const e of ["claude", "codex", "opencode", "agent"]) {
    const c = buildCommand(e, { cwd: "/w", lastMessageFile: "/o" });
    assert.ok(!c.args.includes("--dangerously-skip-permissions"));
    assert.ok(!c.args.includes("--dangerously-bypass-approvals-and-sandbox"));
  }
});

test("claude exit 0 with is_error is a delegate failure", async (t) => {
  const s = sandbox(t);
  s.fake(
    "claude",
    'if [ "$1" = "--version" ]; then echo v; exit 0; fi\ncat >/dev/null; echo \'{"is_error":true,"result":"quota exceeded"}\'',
  );
  const { code, report } = await run(["claude", "--cwd", s.work], {
    env: { ...s.env, ANTHROPIC_API_KEY: "x" },
  });
  assert.equal(code, 1);
  assert.equal(report.executor_error?.kind, "is_error");
  assert.match(report.executor_error?.detail, /quota/);
  assert.equal(report.attempts.length, 1, "quota must not escalate to a more expensive model");
});

test("agent empty JSON result is an invalid execution", async (t) => {
  const s = sandbox(t);
  const authDir = path.join(s.home, ".config", "cursor");
  fs.mkdirSync(authDir, { recursive: true });
  fs.writeFileSync(
    path.join(authDir, "auth.json"),
    '{"accessToken":"sentinel","refreshToken":"stub"}',
  );
  s.fake(
    "agent",
    'if [ "$1" = "--version" ]; then echo v; exit 0; fi\necho \'{"type":"result","result":""}\'',
  );
  const { code, report } = await run(["agent", "--cwd", s.work], {
    env: { ...s.env, HOME: s.home },
  });
  assert.equal(code, 1);
  assert.equal(report.executor_error?.kind, "empty_result");
});

test("read-only agent uses print only with a static-review prompt prefix", async (t) => {
  const s = sandbox(t);
  const authDir = path.join(s.home, ".config", "cursor");
  fs.mkdirSync(authDir, { recursive: true });
  fs.writeFileSync(
    path.join(authDir, "auth.json"),
    '{"accessToken":"sentinel","refreshToken":"stub"}',
  );
  const got = path.join(s.dir, "prompt.txt");
  s.fake(
    "agent",
    `if [ "$1" = "--version" ]; then echo v; exit 0; fi\necho "$@" >${got}; echo '{"type":"result","result":"ok"}'`,
  );
  await run(["agent", "--cwd", s.work, "--read-only"], {
    env: { ...s.env, HOME: s.home },
    input: "inspect only",
  });
  const body = fs.readFileSync(got, "utf8");
  assert.ok(body.includes(READ_ONLY_PROMPT_PREFIX));
  assert.ok(body.includes("inspect only"));
});

test("executorJsonFailure detects empty stdout", () => {
  assert.equal(executorJsonFailure("claude", "").kind, "empty_stdout");
  assert.equal(executorJsonFailure("claude", '{"is_error":false}'), null);
  assert.match(executorJsonFailure("claude", '{"is_error":true,"result":"nope"}').detail, /nope/);
});

test("usage errors exit 64", async (t) => {
  const s = sandbox(t);
  assert.equal((await run(["nonsense"], { env: s.env })).code, 64);
  assert.equal((await run(["claude", "--timeout", "0"], { env: s.env })).code, 64);
});

test("editable chains retry CLI failures with per-attempt durable evidence", async (t) => {
  const s = sandbox(t);
  s.fake(
    "claude",
    `if [ "$1" = "--version" ]; then echo v; exit 0; fi
cat >/dev/null
case "$*" in *sonnet*) echo '{"is_error":true,"result":"context exceeded"}';; *) echo '{"result":"ok"}';; esac`,
  );
  const policy = { harness: { codex: ["luna"] }, implementation: { claude: ["sonnet", "opus"] } };
  const { code, report } = await run(["claude", "--cwd", s.work], {
    env: { ...s.env, ANTHROPIC_API_KEY: "x", ARGUS_MODEL_POLICY: JSON.stringify(policy) },
  });
  assert.equal(code, 0);
  assert.equal(report.engine, "codex");
  assert.equal(report.attempts.length, 2);
  assert.equal(report.attempts[0].configured_model, "sonnet");
  assert.equal(report.attempts[1].configured_model, "opus");
  assert.equal(report.attempts[1].fallback_reason, "is_error");
  for (const attempt of report.attempts) {
    assert.equal(attempt.requested_model, "sonnet");
    assert.equal(attempt.resolved_model, null);
    assert.match(attempt.policy_revision, /^[a-f0-9]{64}$/);
    assert.deepEqual(JSON.parse(fs.readFileSync(attempt.report_file, "utf8")), attempt);
  }
});

test("explicit model pins; failed verification retries are reasoned and task-linked", async (t) => {
  const s = sandbox(t);
  s.fake(
    "claude",
    `if [ "$1" = "--version" ]; then echo v; exit 0; fi
cat >/dev/null; echo '{"is_error":true,"result":"failed"}'`,
  );
  const pinned = await run(["claude", "--cwd", s.work, "--model", "custom"], {
    env: { ...s.env, ANTHROPIC_API_KEY: "x" },
  });
  assert.equal(pinned.code, 1);
  assert.equal(pinned.report.attempts.length, 1);
  assert.equal(pinned.report.requested_model, "custom");
  const { policySnapshot } = await import("./model-policy.mjs");
  const snapshot = policySnapshot(
    { harness: {}, implementation: { claude: ["sonnet", "opus"] } },
    { task_id: "task1", attempt: 3 },
  );
  const file = path.join(s.work, "policy.json");
  const relayFile = path.join(s.dir, "task-log.jsonl");
  fs.writeFileSync(file, JSON.stringify(snapshot));
  const retry = await run(
    ["claude", "--cwd", s.work, "--model-attempt", "2", "--fallback-reason", "verification_failed"],
    {
      env: {
        ...s.env,
        ANTHROPIC_API_KEY: "x",
        ARGUS_MODEL_POLICY_FILE: file,
        ARGUS_TASK_LOG_FILE: relayFile,
      },
    },
  );
  assert.equal(retry.report.configured_model, "opus");
  assert.equal(retry.report.fallback_reason, "verification_failed");
  assert.deepEqual(retry.report.task, { task_id: "task1", attempt: 3 });
  assert.equal(retry.report.attempts.length, 1);
  const records = fs.readFileSync(relayFile, "utf8").trim().split("\n").map(JSON.parse);
  const modelRecs = records.filter((r) => r.models?.executions?.length);
  assert.ok(modelRecs.length >= 2); // running, then terminal
  const terminal = modelRecs.at(-1);
  const execution = terminal.models.executions[0];
  const selection = terminal.models.selections[0];
  assert.equal(terminal.v, 1);
  assert.equal(execution.layer, "delegate");
  assert.equal(execution.executor, "claude");
  assert.equal(execution.configured_model, "opus");
  assert.equal(execution.observed_model, null);
  assert.equal(execution.evidence, "not_reported");
  assert.equal(execution.account_id, null);
  assert.equal(execution.run_key, retry.report.run_key);
  assert.match(execution.key, /^dlg:[0-9a-f]+$/);
  assert.equal(execution.state, "failed");
  assert.equal(selection.kind, "model_switch");
  assert.equal(selection.reason_code, "error");
  assert.equal(selection.to_model, "opus");
});

test("claude observed model comes from the system/init event, else not_reported", async (t) => {
  const s = sandbox(t);
  const terminalExecution = async (stdoutLines) => {
    s.fake(
      "claude",
      `if [ "$1" = "--version" ]; then echo v; exit 0; fi
cat >/dev/null; ${stdoutLines.map((l) => `echo '${l}'`).join("; ")}`,
    );
    const relayFile = path.join(s.dir, `task-log-${Math.random().toString(36).slice(2)}.jsonl`);
    // An explicit policy snapshot carries the task and attempt the relay needs, so the
    // test does not depend on the host's /workspace/agent policy file being present.
    const { policySnapshot } = await import("./model-policy.mjs");
    const snapshot = policySnapshot(
      { harness: {}, implementation: { claude: ["sonnet"] } },
      { task_id: "task1", attempt: 1 },
    );
    const file = path.join(s.dir, "policy.json");
    fs.writeFileSync(file, JSON.stringify(snapshot));
    await run(["claude", "--cwd", s.work], {
      env: {
        ...s.env,
        ANTHROPIC_API_KEY: "x",
        ARGUS_MODEL_POLICY_FILE: file,
        ARGUS_TASK_LOG_FILE: relayFile,
      },
    });
    const records = fs.readFileSync(relayFile, "utf8").trim().split("\n").map(JSON.parse);
    return records.filter((r) => r.models?.executions?.length).at(-1).models.executions[0];
  };
  const init = '{"type":"system","subtype":"init","model":"claude-init-model"}';
  const withInit = await terminalExecution([init, '{"type":"result","result":"ok"}']);
  assert.equal(withInit.observed_model, "claude-init-model");
  assert.equal(withInit.evidence, "cli_output");
  const neither = await terminalExecution(['{"type":"result","result":"ok"}']);
  assert.equal(neither.observed_model, null);
  assert.equal(neither.evidence, "not_reported");
});

test("invalid policies and invalid retry requests fail before CLI execution", async (t) => {
  const s = sandbox(t);
  const marker = path.join(s.dir, "ran");
  s.fake("claude", `touch ${marker}; exit 0`);
  const env = { ...s.env, ANTHROPIC_API_KEY: "x" };
  for (const args of [
    ["--model-attempt", "0"],
    ["--model-attempt", "2"],
    ["--model-attempt", "NaN"],
  ]) {
    assert.equal((await run(["claude", "--cwd", s.work, ...args], { env })).code, 64);
  }
  assert.equal(
    (await run(["claude", "--cwd", s.work], { env: { ...env, ARGUS_MODEL_POLICY: "{}" } })).code,
    64,
  );
  assert.equal(fs.existsSync(marker), false);
});

// -- Claude gateway sentinel across Claude Code's tool-subprocess boundary ----------------

import { createServer } from "node:http";
import { isGatewaySentinel, restoreClaudeGatewayAuth } from "./argus-delegate.mjs";

const SENTINEL = "onecli-managed-claude-sentinel";
const REAL = "sk-ant-" + "oat01-" + "Z".repeat(96); // synthetic: shaped like a real token
const PROXY = "http://agent:proxy-pass-SECRET@127.0.0.1:18080";
const STUB_KEYS = { version: 1, provider: "anthropic", variable: "CLAUDE_CODE_OAUTH_TOKEN" };

/** A gateway stub and the environment of a process running through that gateway. */
function gateway(t, over = {}, envOver = {}) {
  const s = sandbox(t);
  const ca = path.join(s.dir, "ca.pem");
  fs.writeFileSync(ca, "-----BEGIN CERTIFICATE-----\nCA\n-----END CERTIFICATE-----\n");
  const stubPath = path.join(s.dir, "claude-gateway.json");
  const doc = {
    ...STUB_KEYS,
    sentinel: SENTINEL,
    proxy: "127.0.0.1:18080",
    ca,
    ...over,
  };
  fs.writeFileSync(stubPath, JSON.stringify(doc), { mode: 0o644 });
  const env = { ...s.env, HTTPS_PROXY: PROXY, NODE_EXTRA_CA_CERTS: ca, ...envOver };
  return { ...s, ca, stubPath, env, doc };
}

test("claude: rejects an allowed local redirect paired with a cloud oauth token", () => {
  const base = "http://host.docker.internal:8080";
  const out = restoreClaudeGatewayAuth({
    ANTHROPIC_BASE_URL: base,
    ARGUS_ALLOWED_ANTHROPIC_BASE_URL: base,
    CLAUDE_CODE_OAUTH_TOKEN: SENTINEL,
  });
  assert.equal(out.source, null);
  assert.equal(out.stub, "rejected:api_redirected");
  assert.equal(authStatus("claude", out.env).ok, false);
});

test("claude: accepts an exact allowed local redirect with only the harness placeholder", () => {
  const base = "http://host.docker.internal:8080";
  const out = restoreClaudeGatewayAuth({
    ANTHROPIC_BASE_URL: base,
    ARGUS_ALLOWED_ANTHROPIC_BASE_URL: base,
    ANTHROPIC_AUTH_TOKEN: "argus-local-harness",
  });
  assert.equal(out.source, "local_harness");
  assert.equal(authStatus("claude", out.env).ok, true);
});

test("claude: the sentinel is restored only from a host stub that matches the gateway", (t) => {
  const g = gateway(t);
  const out = restoreClaudeGatewayAuth(g.env, g.stubPath);
  assert.equal(out.source, "gateway_sentinel");
  assert.equal(out.env.CLAUDE_CODE_OAUTH_TOKEN, SENTINEL);
  assert.equal(authStatus("claude", out.env, g.home).ok, true);
  assert.equal(g.env.CLAUDE_CODE_OAUTH_TOKEN, undefined, "the input environment is not mutated");
  // authStatus itself stays strict: without the restore step it still fails closed.
  assert.equal(authStatus("claude", g.env, g.home).ok, false);
});

test("claude: no assignment means no stub and a closed failure", (t) => {
  const g = gateway(t);
  const out = restoreClaudeGatewayAuth(g.env, path.join(g.dir, "missing.json"));
  assert.deepEqual([out.source, out.stub], [null, "absent"]);
  assert.equal(out.env.CLAUDE_CODE_OAUTH_TOKEN, undefined);
  assert.equal(authStatus("claude", out.env, g.home).ok, false);
});

test("claude: a real credential in the environment is left alone (no stub involved)", (t) => {
  const g = gateway(t, {}, { ANTHROPIC_API_KEY: "placeholder-key" });
  const out = restoreClaudeGatewayAuth(g.env, g.stubPath);
  assert.equal(out.source, "env");
  assert.equal(out.env.CLAUDE_CODE_OAUTH_TOKEN, undefined);
});

for (const [name, over, envOver, reason] of [
  ["a real-looking token", { sentinel: REAL }, {}, "not_a_sentinel"],
  ["a sentinel with whitespace", { sentinel: "onecli managed" }, {}, "not_a_sentinel"],
  ["an unknown extra key", { extra: "x" }, {}, "malformed"],
  ["another provider", { provider: "openai" }, {}, "malformed"],
  ["another variable", { variable: "ANTHROPIC_API_KEY" }, {}, "malformed"],
  ["a version bump", { version: 2 }, {}, "malformed"],
  ["a different gateway proxy", { proxy: "127.0.0.1:9999" }, {}, "proxy_mismatch"],
  ["no proxy at all", {}, { HTTPS_PROXY: "" }, "proxy_mismatch"],
  ["a missing stub CA", { ca: "/nonexistent/ca.pem" }, {}, "ca_missing"],
  ["no CA in the environment", {}, { NODE_EXTRA_CA_CERTS: "" }, "ca_not_in_environment"],
  ["a redirected API", {}, { ANTHROPIC_BASE_URL: "https://evil.example" }, "api_redirected"],
  ["an API unix socket", {}, { ANTHROPIC_UNIX_SOCKET: "/tmp/x.sock" }, "api_redirected"],
]) {
  test(`claude: fails closed for ${name}`, (t) => {
    const g = gateway(t, over, envOver);
    const out = restoreClaudeGatewayAuth(g.env, g.stubPath);
    assert.equal(out.source, null);
    assert.equal(out.stub, `rejected:${reason}`);
    assert.equal(out.env.CLAUDE_CODE_OAUTH_TOKEN, undefined);
    assert.equal(authStatus("claude", out.env, g.home).ok, false);
    assert.equal(JSON.stringify(out).includes(REAL), false);
  });
}

test("claude: fails closed for a malformed, oversized, writable or symlinked stub", (t) => {
  const g = gateway(t);
  fs.writeFileSync(g.stubPath, "{not json");
  assert.equal(restoreClaudeGatewayAuth(g.env, g.stubPath).stub, "rejected:malformed");
  fs.writeFileSync(g.stubPath, JSON.stringify({ ...g.doc, pad: "x".repeat(5000) }));
  assert.equal(
    restoreClaudeGatewayAuth(g.env, g.stubPath).stub,
    "rejected:not_a_small_regular_file",
  );
  fs.writeFileSync(g.stubPath, JSON.stringify(g.doc));
  fs.chmodSync(g.stubPath, 0o666);
  assert.equal(restoreClaudeGatewayAuth(g.env, g.stubPath).stub, "rejected:writable_by_others");
  fs.chmodSync(g.stubPath, 0o644);
  const link = path.join(g.dir, "link.json");
  fs.symlinkSync(g.stubPath, link);
  assert.equal(restoreClaudeGatewayAuth(g.env, link).stub, "rejected:not_a_small_regular_file");
});

test("isGatewaySentinel mirrors the host's rule", () => {
  assert.equal(isGatewaySentinel(SENTINEL), true);
  assert.equal(isGatewaySentinel("sk-ant-oat01-onecli-managed-placeholder"), true);
  for (const bad of [REAL, "x".repeat(130), "short", "", null, 7, "a b c d e f g h"])
    assert.equal(isGatewaySentinel(bad), false, String(bad));
});

/**
 * What Claude Code 2.1.296 does to its Bash tool's environment: it removes
 * CLAUDE_CODE_OAUTH_TOKEN (and a few other transport tokens) and keeps ANTHROPIC_API_KEY,
 * proxy and CA variables. Verified against the real CLI binary and a local mock API
 * (see the handoff report); reproduced here so the test needs no CLI.
 */
function runShell(command, env, input) {
  return new Promise((resolve) => {
    const child = spawn("bash", ["-lc", command], { env });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (c) => (stdout += c));
    child.stderr.on("data", (c) => (stderr += c));
    child.stdin.end(input);
    child.on("close", (status) => resolve({ status, stdout, stderr }));
  });
}

function claudeToolEnv(env) {
  const out = { ...env };
  for (const name of [
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    "CLAUDE_CODE_ARTIFACTS_API_TOKEN",
  ])
    delete out[name];
  return out;
}

test("boundary: primary env -> sanitized Bash tool -> argus-delegate -> native request through the gateway", async (t) => {
  const g = gateway(t, {}, {});
  // A mock gateway: a forward proxy that sees what the native client sends, swaps in
  // its own "real" credential for the upstream, and answers like the Messages API.
  const seen = [];
  const UPSTREAM_REAL = "gateway-held-REAL-credential";
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      seen.push({
        url: req.url,
        auth: req.headers.authorization,
        upstreamAuth: `Bearer ${UPSTREAM_REAL}`,
      });
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ result: "ok via gateway", is_error: false }));
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => server.close());
  const port = server.address().port;
  const proxy = `http://agent:proxy-pass-SECRET@127.0.0.1:${port}`;
  fs.writeFileSync(g.stubPath, JSON.stringify({ ...g.doc, proxy: `127.0.0.1:${port}` }), {
    mode: 0o644,
  });
  // The `claude` the delegate starts: a native-like client that authenticates with the
  // bearer it finds in CLAUDE_CODE_OAUTH_TOKEN, only through HTTP_PROXY/HTTPS_PROXY.
  g.fake(
    "claude",
    `if [ "$1" = "--version" ]; then echo "9.9.9 (Claude Code)"; exit 0; fi
cat >/dev/null
exec ${process.execPath} -e '
const http = require("node:http");
const proxy = new URL(process.env.HTTPS_PROXY);
const req = http.request({ host: proxy.hostname, port: proxy.port, method: "POST",
  path: "http://api.anthropic.com/v1/messages",
  headers: { host: "api.anthropic.com", authorization: "Bearer " + process.env.CLAUDE_CODE_OAUTH_TOKEN } },
  (res) => { let b = ""; res.on("data", (c) => (b += c)); res.on("end", () => { console.log(b); }); });
req.end("{}");
'`,
  );
  // The driver runs main() with the test's stub path (the production path is fixed).
  const driver = path.join(g.dir, "driver.mjs");
  fs.writeFileSync(
    driver,
    `import { main } from ${JSON.stringify(new URL("./argus-delegate.mjs", import.meta.url).href)};
await main(process.argv.slice(2), process.env, { claudeStub: ${JSON.stringify(g.stubPath)} });`,
  );
  // 1. The primary Claude process has the sentinel; its tool subprocess does not.
  const primary = {
    ...g.env,
    CLAUDE_CODE_OAUTH_TOKEN: SENTINEL,
    HTTPS_PROXY: proxy,
    HTTP_PROXY: proxy,
  };
  const toolEnv = claudeToolEnv(primary);
  assert.equal(toolEnv.CLAUDE_CODE_OAUTH_TOKEN, undefined);
  // 2. The tool runs a login shell that calls the delegate (the contract in the task text).
  const r = await runShell(
    `${process.execPath} ${driver} claude --cwd ${g.work} --timeout 20`,
    toolEnv,
    "do the thing",
  );
  const report = JSON.parse(r.stdout.trim().split("\n").pop());
  assert.equal(r.status, 0, r.stdout + r.stderr);
  assert.equal(report.auth_source, "gateway_sentinel");
  assert.equal(report.blocked, undefined);
  // 3. The native client's request reached the gateway carrying the sentinel only.
  assert.equal(seen.length, 1);
  assert.equal(seen[0].auth, `Bearer ${SENTINEL}`);
  assert.equal(seen[0].url, "http://api.anthropic.com/v1/messages");
  // 4. The gateway's own credential never reached the worker side.
  const everything =
    r.stdout + r.stderr + JSON.stringify(report) + fs.readFileSync(report.artifacts[0], "utf8");
  assert.equal(everything.includes(UPSTREAM_REAL), false);
  assert.equal(everything.includes("proxy-pass-SECRET"), false);
});

test("boundary: the same shell with no gateway stub still fails closed with auth_missing (78)", async (t) => {
  const g = gateway(t);
  g.fake("claude", 'if [ "$1" = "--version" ]; then echo v; exit 0; fi\ntouch ran');
  const driver = path.join(g.dir, "driver.mjs");
  fs.writeFileSync(
    driver,
    `import { main } from ${JSON.stringify(new URL("./argus-delegate.mjs", import.meta.url).href)};
await main(process.argv.slice(2), process.env, { claudeStub: ${JSON.stringify(path.join(g.dir, "absent.json"))} });`,
  );
  const r = await runShell(
    `${process.execPath} ${driver} claude --cwd ${g.work} --timeout 20`,
    claudeToolEnv({ ...g.env, CLAUDE_CODE_OAUTH_TOKEN: SENTINEL }),
    "x",
  );
  const report = JSON.parse(r.stdout.trim().split("\n").pop());
  assert.equal(r.status, 78);
  assert.equal(report.blocked, "auth_missing");
  assert.equal(report.gateway_stub, "absent");
  assert.equal(fs.existsSync(path.join(g.work, "ran")), false, "claude never started");
});

test("withCliToolsPath adds existing tool dirs to PATH and ignores missing ones", () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "clitools-"));
  fs.writeFileSync(path.join(dir, "codex"), "");
  assert.equal(withCliToolsPath({ PATH: "/usr/bin" }, [dir]).PATH, `${dir}:/usr/bin`);
  assert.equal(withCliToolsPath({ PATH: `${dir}:/usr/bin` }, [dir]).PATH, `${dir}:/usr/bin`);
  fs.rmSync(dir, { recursive: true });
  assert.equal(withCliToolsPath({ PATH: "/usr/bin" }, [dir, "/no/such"]).PATH, "/usr/bin");
});

/** A local Argus stand-in that records usage POSTs and answers with `status`. */
async function usageServer(t, status = 204) {
  const calls = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      calls.push({ url: req.url, auth: req.headers.authorization, body: JSON.parse(body) });
      res.statusCode = status;
      res.end();
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => server.close());
  return { calls, url: `http://127.0.0.1:${server.address().port}` };
}

/** A claude stub that prints the real (trimmed) result fixture, with a pinned executor account. */
function usageSandbox(t, context = { task_id: "t1", attempt: 1, executor_account_id: "acc-9" }) {
  const s = sandbox(t);
  const fixture = path.join(s.dir, "claude-result.json");
  fs.copyFileSync(new URL("./fixtures/claude-result.json", import.meta.url), fixture);
  s.fake(
    "claude",
    `if [ "$1" = "--version" ]; then echo v; exit 0; fi\ncat >/dev/null; cat ${fixture}`,
  );
  const policy = { harness: { codex: ["luna"] }, implementation: { claude: ["sonnet", "opus"] } };
  const file = path.join(s.dir, "argus-model-policy.json");
  fs.writeFileSync(file, JSON.stringify(policySnapshot(policy, context)));
  s.env = { ...s.env, ANTHROPIC_API_KEY: "x", ARGUS_MODEL_POLICY_FILE: file };
  s.out = path.join(s.dir, "out");
  return s;
}

test("usage: the report carries parsed usage and a run_key, and a spool file is always written", async (t) => {
  const s = usageSandbox(t);
  const { code, report } = await run(["claude", "--cwd", s.work, "--out", s.out], { env: s.env });
  assert.equal(code, 0);
  assert.equal(report.usage.measurement, "measured");
  assert.equal(report.usage.output_tokens, 18073);
  assert.match(report.run_key, /^r1:t1:.+:codex:claude:/);
  // No Argus env in the container (the host holds the token): the record waits for the host relay.
  assert.deepEqual(report.usage_report, {
    status: "queued",
    reason: "host_relay",
    direct: { status: "skipped", reason: "no_api_env" },
  });
  const [queued, ...rest] = fs
    .readFileSync(path.join(s.dir, "argus-usage.jsonl"), "utf8")
    .trim()
    .split("\n")
    .map((l) => JSON.parse(l));
  assert.equal(rest.length, 0);
  const { payload: queuedPayload, ...envelope } = queued;
  assert.deepEqual(envelope, {
    v: 1,
    kind: "usage",
    source: "delegate",
    role: "executor",
    task_id: "t1",
    attempt: 1,
    account_id: "acc-9",
  });
  assert.equal(queuedPayload.run_key, report.run_key);
  assert.equal(queuedPayload.measurement, "measured");
  const spool = JSON.parse(fs.readFileSync(report.usage_spool, "utf8"));
  assert.equal(path.dirname(report.usage_spool), s.out);
  assert.equal(spool.account_id, "acc-9");
  assert.equal(spool.payload.run_key, report.run_key);
  assert.deepEqual(JSON.parse(fs.readFileSync(report.report_file, "utf8")).usage, report.usage);
});

test("usage: POSTs to the executor account with the bearer token only in the header", async (t) => {
  const s = usageSandbox(t);
  const api = await usageServer(t);
  const { code, report } = await run(["claude", "--cwd", s.work, "--out", s.out], {
    env: { ...s.env, ARGUS_URL: api.url, ARGUS_AGENT_TOKEN: "tok-secret-123" },
  });
  assert.equal(code, 0);
  assert.equal(api.calls.length, 1);
  const [call] = api.calls;
  assert.equal(call.url, "/api/accounts/acc-9/usage");
  assert.equal(call.auth, "Bearer tok-secret-123");
  assert.deepEqual(call.body, {
    task_id: "t1",
    attempt: 1,
    model: "claude-sonnet-5-5",
    engine: "claude",
    role: "executor",
    measurement: "measured",
    input_tokens: 61637,
    output_tokens: 18073,
    cached_input_tokens: 1156589,
    cost_usd: 0.5325657,
    reason: null,
    run_key: report.run_key,
  });
  assert.deepEqual(report.usage_report, { status: "sent", reason: "recorded" });
  // Sent directly: nothing is queued for the host (no second report of the same run).
  assert.equal(fs.existsSync(path.join(s.dir, "argus-usage.jsonl")), false);
  for (const text of [
    JSON.stringify(call.body),
    JSON.stringify(report),
    fs.readFileSync(report.usage_spool, "utf8"),
  ])
    assert.equal(text.includes("tok-secret-123"), false);
});

test("usage: a failing or unreachable API never changes the exit code", async (t) => {
  const s = usageSandbox(t);
  const api = await usageServer(t, 500);
  const failed = await run(["claude", "--cwd", s.work, "--out", s.out], {
    env: { ...s.env, ARGUS_URL: api.url, ARGUS_AGENT_TOKEN: "tok" },
  });
  assert.equal(failed.code, 0);
  // A failed direct report falls back to the host relay; the run_key makes a double report harmless.
  assert.deepEqual(failed.report.usage_report, {
    status: "queued",
    reason: "host_relay",
    direct: { status: "failed", reason: "http_500" },
  });
  const down = await run(["claude", "--cwd", s.work, "--out", s.out], {
    env: { ...s.env, ARGUS_URL: "http://127.0.0.1:1", ARGUS_AGENT_TOKEN: "tok" },
  });
  assert.equal(down.code, 0);
  assert.equal(down.report.usage_report.direct.status, "failed");
  assert.equal(fs.existsSync(down.report.usage_spool), true);
});

test("usage: never POSTs without an executor account id", async (t) => {
  const s = usageSandbox(t, { task_id: "t1", attempt: 1, engine_account_id: "acc-engine" });
  const api = await usageServer(t);
  const { report } = await run(["claude", "--cwd", s.work, "--out", s.out], {
    env: { ...s.env, ARGUS_URL: api.url, ARGUS_AGENT_TOKEN: "tok" },
  });
  assert.deepEqual(report.usage_report, { status: "skipped", reason: "no_account_id" });
  assert.equal(api.calls.length, 0);
});

test("usage: output without figures is reported not_reported with null figures", async (t) => {
  const s = sandbox(t);
  s.fake(
    "claude",
    'if [ "$1" = "--version" ]; then echo v; exit 0; fi\ncat >/dev/null; echo \'{"result":"hi"}\'',
  );
  const policy = { harness: {}, implementation: { claude: ["sonnet"] } };
  const file = path.join(s.dir, "policy.json");
  fs.writeFileSync(
    file,
    JSON.stringify(
      policySnapshot(policy, { task_id: "t2", attempt: 1, executor_account_id: "acc-1" }),
    ),
  );
  const api = await usageServer(t);
  const { code, report } = await run(
    ["claude", "--cwd", s.work, "--out", path.join(s.dir, "out")],
    {
      env: {
        ...s.env,
        ANTHROPIC_API_KEY: "x",
        ARGUS_MODEL_POLICY_FILE: file,
        ARGUS_URL: api.url,
        ARGUS_AGENT_TOKEN: "tok",
      },
    },
  );
  assert.equal(code, 0);
  assert.equal(report.usage.measurement, "not_reported");
  const [call] = api.calls;
  assert.equal(call.body.measurement, "not_reported");
  assert.equal(call.body.reason, "no_result_object");
  for (const k of ["input_tokens", "output_tokens", "cached_input_tokens", "cost_usd"])
    assert.equal(call.body[k], null);
});

test("usage: each model attempt is its own run with its own key and spool file", async (t) => {
  const s = sandbox(t);
  s.fake(
    "claude",
    `if [ "$1" = "--version" ]; then echo v; exit 0; fi
cat >/dev/null
case "$*" in *sonnet*) echo '{"is_error":true,"result":"context exceeded"}';; *) echo '{"result":"ok","usage":{"input_tokens":3,"output_tokens":4}}';; esac`,
  );
  const policy = { harness: {}, implementation: { claude: ["sonnet", "opus"] } };
  const file = path.join(s.dir, "policy.json");
  fs.writeFileSync(
    file,
    JSON.stringify(
      policySnapshot(policy, { task_id: "t3", attempt: 1, executor_account_id: "acc-1" }),
    ),
  );
  const api = await usageServer(t);
  const { report } = await run(["claude", "--cwd", s.work, "--out", path.join(s.dir, "out")], {
    env: {
      ...s.env,
      ANTHROPIC_API_KEY: "x",
      ARGUS_MODEL_POLICY_FILE: file,
      ARGUS_URL: api.url,
      ARGUS_AGENT_TOKEN: "tok",
    },
  });
  assert.equal(report.attempts.length, 2);
  assert.equal(api.calls.length, 2);
  assert.notEqual(report.attempts[0].run_key, report.attempts[1].run_key);
  assert.notEqual(report.attempts[0].usage_spool, report.attempts[1].usage_spool);
  assert.deepEqual(
    api.calls.map((c) => c.body.measurement),
    ["not_reported", "measured"],
  );
  assert.deepEqual(
    api.calls.map((c) => c.body.model),
    ["sonnet", "opus"],
  );
});

// ---- custom OpenAI-compatible account settings (docs/specs/custom-accounts.spec.md) ----

const KEY = "sk-or-v1-TOPSECRETKEY0123456789"; // gitleaks:allow (test fixture)
const SETTINGS = {
  base_url: "http://127.0.0.1:9/api/v1",
  label: "openrouter",
  model: "qwen/qwen3-coder",
  provider: { order: ["deepinfra"], allow_fallbacks: false, only: ["deepinfra"] },
};

/** A stub executor that records argv and the env it was given, then answers. */
function customSandbox(t, executor, { settings = SETTINGS, policy } = {}) {
  const s = sandbox(t);
  const rec = path.join(s.dir, "rec");
  s.fake(
    executor,
    `if [ "$1" = "--version" ]; then echo v; exit 0; fi
printf '%s\\n' "$@" > ${rec}.argv
env > ${rec}.env
cat >/dev/null; echo '{"result":"ok"}'`,
  );
  const file = path.join(s.dir, "argus-model-policy.json");
  const chains = policy ?? {
    harness: { codex: ["luna"] },
    implementation: { codex: ["policy-model"], opencode: ["policy-model"] },
  };
  fs.writeFileSync(
    file,
    JSON.stringify(
      policySnapshot(chains, { task_id: "t1", attempt: 1, executor_settings: settings }),
    ),
  );
  s.env = { ...s.env, [CUSTOM_KEY_ENV]: KEY, ARGUS_MODEL_POLICY_FILE: file };
  s.out = path.join(s.dir, "out");
  s.read = (n) => fs.readFileSync(`${rec}.${n}`, "utf8");
  return s;
}
const dump = (report) => JSON.stringify(report);

test("custom account: OpenCode gets base_url, model and provider block; the key stays out of argv, output and report", async (t) => {
  const s = customSandbox(t, "opencode");
  const { code, report } = await run(["opencode", "--cwd", s.work, "--out", s.out], { env: s.env });
  assert.equal(code, 0);
  const argv = s.read("argv");
  assert.match(argv, /^-m\nargus_custom\/qwen\/qwen3-coder$/m);
  assert.ok(!argv.includes(KEY));
  const env = s.read("env");
  const cfg = JSON.parse(env.match(/^OPENCODE_CONFIG_CONTENT=(.*)$/m)[1]);
  const entry = cfg.provider.argus_custom;
  assert.equal(entry.options.baseURL, "http://127.0.0.1:9/api/v1");
  assert.equal(entry.options.apiKey, "{env:ARGUS_ACCOUNT_API_KEY}");
  assert.deepEqual(entry.models["qwen/qwen3-coder"].options.provider, SETTINGS.provider);
  assert.ok(!env.match(/^OPENCODE_CONFIG_CONTENT=.*/m)[0].includes(KEY));
  assert.ok(env.includes(`ARGUS_ACCOUNT_API_KEY=${KEY}`)); // only via env
  assert.equal(report.model, "qwen/qwen3-coder");
  assert.equal(report.model_source, "account");
  assert.equal(report.provider_block_applied, true);
  assert.ok(!dump(report).includes(KEY));
  for (const f of [...report.artifacts, report.report_file, report.usage_spool])
    assert.ok(!fs.readFileSync(f, "utf8").includes(KEY), f);
});

test("custom account: Codex takes base_url and model but cannot carry the provider block", async (t) => {
  const s = customSandbox(t, "codex");
  const { code, report } = await run(["codex", "--cwd", s.work, "--out", s.out], { env: s.env });
  assert.equal(code, 0);
  const argv = s.read("argv");
  assert.match(argv, /^-m\nqwen\/qwen3-coder$/m);
  assert.ok(argv.includes('model_providers.argus_custom.base_url="http://127.0.0.1:9/api/v1"'));
  assert.ok(argv.includes('model_providers.argus_custom.env_key="ARGUS_ACCOUNT_API_KEY"'));
  assert.ok(!argv.includes(KEY));
  assert.ok(!argv.includes("deepinfra"));
  assert.equal(report.provider_block_applied, false);
  assert.ok(!dump(report).includes(KEY));
});

test("custom account: the key is redacted if the executor echoes it", async (t) => {
  const s = customSandbox(t, "opencode");
  s.fake(
    "opencode",
    `if [ "$1" = "--version" ]; then echo v; exit 0; fi\ncat >/dev/null; echo "bad key $ARGUS_ACCOUNT_API_KEY" >&2; echo "boom $ARGUS_ACCOUNT_API_KEY"; exit 3`,
  );
  const { code, report } = await run(["opencode", "--cwd", s.work, "--out", s.out], { env: s.env });
  assert.equal(code, 1);
  assert.ok(!dump(report).includes(KEY));
  assert.match(report.stderr_tail, /bad key \[redacted\]/);
});

test("custom account: a missing key blocks with auth_missing and nothing runs", async (t) => {
  const s = customSandbox(t, "opencode");
  delete s.env[CUSTOM_KEY_ENV];
  const { code, report } = await run(["opencode", "--cwd", s.work], { env: s.env });
  assert.equal(code, 78);
  assert.equal(report.blocked, "auth_missing");
  assert.match(report.need, /ARGUS_ACCOUNT_API_KEY/);
});

test("custom account: claude and agent cannot use it", async (t) => {
  const s = customSandbox(t, "claude");
  const { code, report } = await run(["claude", "--cwd", s.work], { env: s.env });
  assert.equal(code, 64);
  assert.match(report.detail, /cannot use a custom account/);
});

test("model precedence: --model, then account model, then policy chain, then native default", async (t) => {
  const go = async (executor, args, settings, policy) => {
    const s = customSandbox(t, executor, { settings, policy });
    const r = await run([executor, "--cwd", s.work, "--out", s.out, ...args], { env: s.env });
    return r.report;
  };
  let r = await go("opencode", ["--model", "cli/model"], SETTINGS);
  assert.deepEqual([r.model, r.model_source], ["cli/model", "cli"]);
  r = await go("opencode", [], SETTINGS);
  assert.deepEqual([r.model, r.model_source], ["qwen/qwen3-coder", "account"]);
  r = await go("opencode", [], { ...SETTINGS, model: null });
  assert.deepEqual([r.model, r.model_source], ["policy-model", "policy"]);
  r = await go(
    "opencode",
    [],
    { ...SETTINGS, model: null },
    { harness: { codex: ["l"] }, implementation: { codex: ["x"] } },
  );
  assert.deepEqual([r.model, r.model_source], [null, "native_default"]);
});

test("account settings are validated and never echoed", () => {
  assert.equal(parseAccountSettings(undefined), null);
  assert.equal(parseAccountSettings(JSON.stringify(SETTINGS)).base_url, SETTINGS.base_url);
  assert.throws(() => parseAccountSettings({ base_url: "https://user:pw@h/v1" }), /credentials/);
  assert.throws(() => parseAccountSettings({ base_url: "https://h/v1?key=abc" }), /query/);
  assert.throws(
    () => parseAccountSettings({ base_url: "https://h/v1", model: "bad model" }),
    /model/,
  );
});

test("a fake OpenAI-compatible server sees the key as bearer and the provider block in the body", async (t) => {
  const seen = [];
  const server = http.createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      seen.push({ auth: req.headers.authorization, body: JSON.parse(body) });
      res.setHeader("content-type", "application/json");
      res.end(JSON.stringify({ choices: [{ message: { content: "ok" } }] }));
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  t.after(() => server.close());
  const base = `http://127.0.0.1:${server.address().port}/api/v1`;
  const s = customSandbox(t, "opencode", { settings: { ...SETTINGS, base_url: base } });
  // The stub executor plays OpenCode: it reads the injected config and calls the endpoint.
  s.fake(
    "opencode",
    `if [ "$1" = "--version" ]; then echo v; exit 0; fi
cat >/dev/null
node -e '
const c = JSON.parse(process.env.OPENCODE_CONFIG_CONTENT).provider.argus_custom;
const model = Object.keys(c.models)[0];
const key = c.options.apiKey.replace(/^\\{env:(.*)\\}$/, (_, n) => process.env[n]);
fetch(c.options.baseURL + "/chat/completions", { method: "POST", headers: { authorization: "Bearer " + key, "content-type": "application/json" },
  body: JSON.stringify({ model, provider: c.models[model].options.provider, messages: [] }) }).then(() => console.log(JSON.stringify({ result: "ok" })));
'`,
  );
  const { code, report } = await run(["opencode", "--cwd", s.work, "--out", s.out], { env: s.env });
  assert.equal(code, 0);
  assert.equal(seen.length, 1);
  assert.equal(seen[0].auth, `Bearer ${KEY}`);
  assert.equal(seen[0].body.model, "qwen/qwen3-coder");
  assert.deepEqual(seen[0].body.provider, SETTINGS.provider);
  assert.ok(!dump(report).includes(KEY));
});

/** A fake claude that prints a stream-json run; `extra` is shell run before the result line. */
function liveRun(t, { extra = "", leakInEnv = {}, envExtra = {}, relayFile } = {}) {
  const s = sandbox(t);
  const ev = (o) => `echo '${JSON.stringify(o)}'`;
  s.fake(
    "claude",
    [
      'if [ "$1" = "--version" ]; then echo v; exit 0; fi',
      "cat >/dev/null",
      `env > ${path.join(s.dir, "child-env.txt")}`,
      ev({ type: "system", subtype: "init", model: "m-1" }),
      ev({
        type: "assistant",
        message: {
          content: [
            { type: "text", text: "Reading the spec" },
            { type: "tool_use", name: "Read", input: { file_path: "/w/a.py" } },
            { type: "text", text: "Authorization: Bearer abcdef1234567890xyz" },
          ],
        },
      }),
      extra,
      ev({ type: "result", subtype: "success", is_error: false, result: "done", usage: {} }),
    ].join("\n"),
  );
  const policy = JSON.parse(
    fs.readFileSync(new URL("./model-policy.defaults.json", import.meta.url), "utf8"),
  );
  const file = path.join(s.dir, "argus-model-policy.json");
  fs.writeFileSync(file, JSON.stringify(policySnapshot(policy, { task_id: "t1", attempt: 3 })));
  const env = {
    ...s.env,
    ANTHROPIC_API_KEY: "placeholder",
    ARGUS_MODEL_POLICY_FILE: file,
    ARGUS_LIVELOG_TICK_S: "0.05",
    ARGUS_LIVELOG_FLUSH_S: "0.1",
    ...(relayFile ? { ARGUS_TASK_LOG_FILE: relayFile } : {}),
    ...leakInEnv,
    ...envExtra,
  };
  const relay = relayFile ?? path.join(s.dir, "argus-task-log.jsonl");
  const records = () =>
    fs.existsSync(relay)
      ? fs
          .readFileSync(relay, "utf8")
          .trim()
          .split("\n")
          .filter(Boolean)
          .map((l) => JSON.parse(l))
      : [];
  return { s, env, relay, records };
}

test("a claude run streams readable, redacted lines to the relay file beside the policy", async (t) => {
  const l = liveRun(t);
  const { code, report } = await run(["claude", "--cwd", l.s.work, "--timeout", "20"], {
    env: l.env,
  });
  assert.equal(code, 0);
  assert.equal(report.exit_code, 0);
  assert.equal(report.executor_error, undefined);
  const lines = l.records().flatMap((r) => (Array.isArray(r.lines) ? r.lines : []));
  assert.deepEqual(lines.slice(0, 3), [
    "claude started (m-1)",
    "Reading the spec",
    "▸ Read /w/a.py",
  ]);
  assert.equal(lines[3], "[redacted]");
  assert.equal(lines.at(-1), "claude finished");
  assert.ok(
    l.records().every((r) => r.task_id === "t1" && r.attempt === 3 && (r.v === 1 || r.models)),
  );
  // The raw stream stays the artifact and its last line is the final report.
  const out = fs.readFileSync(report.artifacts[0], "utf8").trim().split("\n");
  assert.equal(JSON.parse(out.at(-1)).type, "result");
  assert.match(report.tail, /"result":"done"/);
});

test("a silent executor produces keepalive lines", async (t) => {
  const l = liveRun(t, {
    extra: "sleep 1.2",
    envExtra: { ARGUS_LIVELOG_KEEPALIVE_S: "0.3" },
  });
  const { code } = await run(["claude", "--cwd", l.s.work, "--timeout", "20"], { env: l.env });
  assert.equal(code, 0);
  const alive = l
    .records()
    .flatMap((r) => r.lines)
    .filter((x) => /^delegate alive, \d+s since last output$/.test(x));
  assert.ok(alive.length >= 2, JSON.stringify(l.records()));
});

test("a broken relay never fails the run", async (t) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "norelay-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.writeFileSync(path.join(dir, "file"), "x");
  // a path below a regular file can never be created
  const l = liveRun(t, { relayFile: path.join(dir, "file", "sub", "log.jsonl") });
  const { code, report } = await run(["claude", "--cwd", l.s.work, "--timeout", "20"], {
    env: l.env,
  });
  assert.equal(code, 0);
  assert.equal(report.exit_code, 0);
  assert.equal(fs.existsSync(l.relay), false);
});

test("values of secret-looking env vars are masked even without a known shape", async (t) => {
  const l = liveRun(t, {
    extra:
      "echo '" +
      JSON.stringify({
        type: "assistant",
        message: { content: [{ type: "text", text: "the db said opensesame-2026 back" }] },
      }) +
      "'",
    envExtra: { DB_PASSWORD: "opensesame-2026" },
  });
  const { code } = await run(["claude", "--cwd", l.s.work, "--timeout", "20"], { env: l.env });
  assert.equal(code, 0);
  const relayed = fs.readFileSync(l.relay, "utf8");
  assert.equal(relayed.includes("opensesame-2026"), false);
  assert.match(relayed, /the db said \[redacted\] back/);
});

test("the Argus agent token is neither passed on nor relayed", async (t) => {
  const TOKEN = "argus_agent_SECRET0123456789";
  // The channel keeps the token on the host; a delegate started without it never gains one.
  const l = liveRun(t, {
    extra:
      "echo '" +
      JSON.stringify({
        type: "assistant",
        message: { content: [{ type: "text", text: `leak ARGUS_AGENT_TOKEN=${TOKEN}` }] },
      }) +
      "'",
  });
  const { code } = await run(["claude", "--cwd", l.s.work, "--timeout", "20"], { env: l.env });
  assert.equal(code, 0);
  assert.equal(
    /ARGUS_AGENT_TOKEN=/.test(fs.readFileSync(path.join(l.s.dir, "child-env.txt"), "utf8")),
    false,
  );
  const relayed = fs.readFileSync(l.relay, "utf8");
  assert.equal(relayed.includes(TOKEN), false);
  assert.match(relayed, /ARGUS_AGENT_TOKEN=\[redacted\]/);
  // and the relay path itself makes no request: nothing in it can authenticate
  const src = fs.readFileSync(new URL("./livelog.mjs", import.meta.url), "utf8");
  assert.doesNotMatch(src, /ARGUS_AGENT_TOKEN|fetch\(/);
});

test("Cursor gets composer-2.5 from the trivial map for trivial tasks and local leases only", async (t) => {
  const defaults = JSON.parse(
    fs.readFileSync(new URL("./model-policy.defaults.json", import.meta.url), "utf8"),
  );
  const go = async (context) => {
    const s = sandbox(t);
    const rec = path.join(s.dir, "argv");
    s.fake(
      "agent",
      `if [ "$1" = "--version" ]; then echo v; exit 0; fi\nprintf '%s\\n' "$@" > ${rec}\necho '{"result":"ok"}'`,
    );
    const cursor = path.join(s.home, ".config", "cursor");
    fs.mkdirSync(cursor, { recursive: true });
    fs.writeFileSync(path.join(cursor, "auth.json"), '{"accessToken":"sentinel"}');
    const file = path.join(s.dir, "argus-model-policy.json");
    fs.writeFileSync(file, JSON.stringify(policySnapshot(defaults, { task_id: "t1", ...context })));
    const env = { ...s.env, ARGUS_MODEL_POLICY_FILE: file };
    const { code, report } = await run(
      ["agent", "--cwd", s.work, "--out", path.join(s.dir, "out"), "--timeout", "20"],
      { env },
    );
    assert.equal(code, 0);
    return { report, argv: fs.readFileSync(rec, "utf8") };
  };
  for (const context of [
    { complexity: "trivial", account_provider: "anthropic" },
    { complexity: "standard", account_provider: "local" },
  ]) {
    const { report, argv } = await go(context);
    assert.match(argv, /^--model\ncomposer-2\.5$/m);
    assert.deepEqual(
      [report.model, report.model_source, report.policy_map],
      ["composer-2.5", "policy", "trivial"],
    );
  }
  const { report, argv } = await go({ complexity: "standard", account_provider: "anthropic" });
  assert.ok(!argv.includes("--model"));
  assert.deepEqual(
    [report.model, report.model_source, report.policy_map],
    [null, "native_default", "implementation"],
  );
});

test("a local harness never starts a Claude Code executor (Strata serves only the harness)", async (t) => {
  const box = sandbox(t);
  box.fake("claude", "echo should-not-run; exit 0");
  const env = {
    ...box.env,
    ARGUS_ENGINE: "claude",
    ANTHROPIC_BASE_URL: "http://host.docker.internal:8080",
    ARGUS_ALLOWED_ANTHROPIC_BASE_URL: "http://host.docker.internal:8080",
    ANTHROPIC_AUTH_TOKEN: "argus-local-harness",
    ARGUS_LOCAL_HARNESS: "1",
  };
  const { code, report } = await run(["claude", "--cwd", box.work], { env });
  assert.equal(code, 78);
  assert.equal(report.blocked, "auth_missing");
  assert.equal(report.reason, "local_harness_engine_only");
  assert.match(report.need, /EXECUTOR agent/);
});

test("localHarnessRefusal spares other executors and cloud harnesses", () => {
  const local = { ARGUS_LOCAL_HARNESS: "1" };
  assert.equal(localHarnessRefusal("agent", local), null);
  assert.equal(localHarnessRefusal("codex", local), null);
  assert.equal(localHarnessRefusal("opencode", local), null);
  assert.notEqual(localHarnessRefusal("claude", local), null);
  // older env without the marker: the local placeholder token on a redirect
  assert.notEqual(
    localHarnessRefusal("claude", {
      ANTHROPIC_BASE_URL: "http://host.docker.internal:8080",
      ANTHROPIC_AUTH_TOKEN: "argus-local-harness",
    }),
    null,
  );
  assert.equal(localHarnessRefusal("claude", { CLAUDE_CODE_OAUTH_TOKEN: "x" }), null);
});

// -- Custom harness (OpenRouter bridge): the executor never rides the bridge -------------

import { customHarnessActive, withoutCustomHarness } from "./argus-delegate.mjs";

const BRIDGE = "http://host.docker.internal:32776";
const BRIDGE_KEY = "a".repeat(64);
/** What withCustomHarnessRuntime puts in the container env (custom harness spec). */
const customHarnessEnv = () => ({
  ANTHROPIC_BASE_URL: BRIDGE,
  ARGUS_ALLOWED_ANTHROPIC_BASE_URL: BRIDGE,
  ANTHROPIC_AUTH_TOKEN: "argus-custom-harness",
  ANTHROPIC_MODEL: "deepseek/deepseek-v4.1-flash-1017",
  ANTHROPIC_CUSTOM_HEADERS: `x-argus-bridge-key: ${BRIDGE_KEY}`,
  ARGUS_CUSTOM_HARNESS: "1",
});

/** A driver that runs main() with the test's Claude gateway stub path. */
function stubDriver(g, stubPath) {
  const driver = path.join(g.dir, "driver.mjs");
  fs.writeFileSync(
    driver,
    `import { main } from ${JSON.stringify(new URL("./argus-delegate.mjs", import.meta.url).href)};
await main(process.argv.slice(2), process.env, { claudeStub: ${JSON.stringify(stubPath)} });`,
  );
  return driver;
}

test("customHarnessActive: the marker, or the custom placeholder on a redirect", () => {
  assert.equal(customHarnessActive(customHarnessEnv()), true);
  assert.equal(
    customHarnessActive({
      ANTHROPIC_BASE_URL: BRIDGE,
      ANTHROPIC_AUTH_TOKEN: "argus-custom-harness",
    }),
    true,
  );
  assert.equal(
    customHarnessActive({ ARGUS_LOCAL_HARNESS: "1", ANTHROPIC_BASE_URL: BRIDGE }),
    false,
  );
  assert.equal(customHarnessActive({ CLAUDE_CODE_OAUTH_TOKEN: "x" }), false);
  assert.equal(customHarnessActive({ ANTHROPIC_AUTH_TOKEN: "argus-custom-harness" }), false);
});

test("withoutCustomHarness removes every bridge variable and keeps the rest", () => {
  const out = withoutCustomHarness({
    ...customHarnessEnv(),
    HTTPS_PROXY: "http://p:1",
    PATH: "/b",
  });
  assert.deepEqual(out, { HTTPS_PROXY: "http://p:1", PATH: "/b" });
});

test("custom harness: argus-delegate claude without an Anthropic executor lease refuses (nothing runs)", async (t) => {
  const g = gateway(t);
  g.fake("claude", 'if [ "$1" = "--version" ]; then echo v; exit 0; fi\ntouch ran');
  const driver = stubDriver(g, path.join(g.dir, "absent.json"));
  const r = await runShell(
    `${process.execPath} ${driver} claude --cwd ${g.work} --timeout 20`,
    { ...g.env, ARGUS_ENGINE: "claude", ...customHarnessEnv() },
    "x",
  );
  const report = JSON.parse(r.stdout.trim().split("\n").pop());
  assert.equal(r.status, 78, r.stdout + r.stderr);
  assert.equal(report.blocked, "auth_missing");
  assert.equal(report.reason, "custom_harness_engine_only");
  assert.equal(report.harness, "custom");
  assert.match(report.need, /EXECUTOR agent/);
  assert.equal(fs.existsSync(path.join(g.work, "ran")), false, "claude never started");
  assert.equal((r.stdout + r.stderr).includes(BRIDGE_KEY), false);
});

test("custom harness: argus-delegate claude runs on the gateway's Anthropic account, never the bridge", async (t) => {
  const g = gateway(t);
  const dump = path.join(g.dir, "child-env");
  g.fake(
    "claude",
    `if [ "$1" = "--version" ]; then echo "9.9.9 (Claude Code)"; exit 0; fi
cat >/dev/null
env > ${JSON.stringify(dump)}
echo '{"result":"ok","is_error":false}'`,
  );
  const driver = stubDriver(g, g.stubPath);
  // Claude Code's Bash tool drops CLAUDE_CODE_OAUTH_TOKEN but passes the bridge env on;
  // the delegate must remove it before starting the child.
  const r = await runShell(
    `${process.execPath} ${driver} claude --cwd ${g.work} --timeout 20`,
    claudeToolEnv({ ...g.env, ARGUS_ENGINE: "claude", ...customHarnessEnv() }),
    "do the thing",
  );
  const report = JSON.parse(r.stdout.trim().split("\n").pop());
  assert.equal(r.status, 0, r.stdout + r.stderr);
  assert.equal(report.auth_source, "gateway_sentinel");
  assert.equal(report.harness, "custom");
  const child = Object.fromEntries(
    fs
      .readFileSync(dump, "utf8")
      .split("\n")
      .filter(Boolean)
      .map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]),
  );
  for (const name of [
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ARGUS_ALLOWED_ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ARGUS_CUSTOM_HARNESS",
  ])
    assert.equal(child[name], undefined, name);
  assert.equal(child.CLAUDE_CODE_OAUTH_TOKEN, SENTINEL);
  assert.equal(JSON.stringify(report).includes(BRIDGE_KEY), false);
});

test("custom harness: other executors never see the bridge either (codex)", async (t) => {
  const s = sandbox(t);
  const dump = path.join(s.dir, "child-env");
  s.fake(
    "codex",
    `if [ "$1" = "--version" ]; then echo codex-cli 0; exit 0; fi
cat >/dev/null
env > ${JSON.stringify(dump)}`,
  );
  const { report } = await run(["codex", "--cwd", s.work, "--timeout", "20"], {
    env: { ...s.env, OPENAI_API_KEY: "x", ...customHarnessEnv() },
  });
  assert.equal(report.harness, "custom");
  const child = fs.readFileSync(dump, "utf8");
  for (const name of [
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ARGUS_ALLOWED_ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ARGUS_CUSTOM_HARNESS",
  ])
    assert.equal(new RegExp(`^${name}=`, "m").test(child), false, name);
  assert.equal(child.includes(BRIDGE_KEY), false);
});

/** A fake Cursor `agent` whose body is `script({ e, relay, dir })`, with auth, policy and relay. */
function agentRun(t, script) {
  const s = sandbox(t);
  const e = (o) => `echo '${JSON.stringify(o)}'`;
  const relay = path.join(s.dir, "argus-task-log.jsonl");
  s.fake(
    "agent",
    [
      'if [ "$1" = "--version" ]; then echo v; exit 0; fi',
      ...script({ e, relay, dir: s.dir }),
    ].join("\n"),
  );
  const cursor = path.join(s.home, ".config", "cursor");
  fs.mkdirSync(cursor, { recursive: true });
  fs.writeFileSync(path.join(cursor, "auth.json"), '{"accessToken":"sentinel"}');
  const policy = JSON.parse(
    fs.readFileSync(new URL("./model-policy.defaults.json", import.meta.url), "utf8"),
  );
  const file = path.join(s.dir, "argus-model-policy.json");
  fs.writeFileSync(file, JSON.stringify(policySnapshot(policy, { task_id: "t1", attempt: 1 })));
  const env = {
    ...s.env,
    ARGUS_MODEL_POLICY_FILE: file,
    ARGUS_TASK_LOG_FILE: relay,
    ARGUS_LIVELOG_TICK_S: "0.05",
    ARGUS_LIVELOG_FLUSH_S: "0.1",
  };
  const start = (timeout) =>
    run(["agent", "--cwd", s.work, "--out", path.join(s.dir, "out"), "--timeout", timeout], {
      env,
    });
  const lines = () =>
    fs
      .readFileSync(relay, "utf8")
      .trim()
      .split("\n")
      .flatMap((l) => JSON.parse(l).lines ?? []);
  return { s, relay, start, lines };
}

const AGENT_INIT = { type: "system", subtype: "init", model: "Composer" };
const AGENT_READ = {
  type: "tool_call",
  subtype: "started",
  call_id: "1",
  tool_call: { readToolCall: { args: { path: "/w/a.py" } } },
};
const AGENT_RESULT = {
  type: "result",
  subtype: "success",
  is_error: false,
  result: "all done",
  usage: { inputTokens: 10, outputTokens: 5, cacheReadTokens: 2, cacheWriteTokens: 0 },
};

test("a cursor agent run streams progress while running and keeps the final result and usage", async (t) => {
  let marker;
  const a = agentRun(t, ({ e, relay, dir }) => {
    marker = path.join(dir, "progress-seen");
    return [
      e(AGENT_INIT),
      e({
        type: "user",
        message: {
          content: [{ type: "text", text: "SECRET-PROMPT https://github.com/o/r/pull/99" }],
        },
      }),
      e({ type: "thinking", subtype: "delta", text: "hidden thoughts" }),
      e(AGENT_READ),
      // wait until the delegate's live log has shown the progress, then finish
      `i=0; while [ $i -lt 100 ] && ! grep -q 'read /w/a.py' ${relay} 2>/dev/null; do sleep 0.1; i=$((i+1)); done`,
      `grep -q 'read /w/a.py' ${relay} && touch ${marker}`,
      e(AGENT_RESULT),
      e({ type: "system", subtype: "task_notification", status: "done" }),
    ];
  });
  const { code, report } = await a.start("30");
  assert.equal(code, 0);
  assert.equal(fs.existsSync(marker), true, "progress was not relayed before the run ended");
  assert.equal(report.executor_error, undefined);
  assert.equal(report.usage.measurement, "measured");
  assert.deepEqual(
    [report.usage.input_tokens, report.usage.output_tokens, report.usage.cached_input_tokens],
    [10, 5, 2],
  );
  assert.match(report.tail, /"result":"all done"/);
  const lines = a.lines();
  assert.deepEqual(lines.slice(0, 2), ["agent started (Composer)", "▸ read /w/a.py"]);
  assert.equal(lines.at(-1), "agent finished");
  const relayed = lines.join("\n");
  assert.equal(/SECRET-PROMPT|hidden thoughts/.test(relayed), false);
});

test("a cursor agent that hangs mid-stream times out with 124, progress relayed and its tree killed", async (t) => {
  let pidFile;
  const a = agentRun(t, ({ e, dir }) => {
    pidFile = path.join(dir, "grandchild.pid");
    return [e(AGENT_INIT), e(AGENT_READ), `sleep 30 &\necho $! > ${pidFile}\nwait`];
  });
  const started = Date.now();
  const { code, report } = await a.start("1");
  assert.equal(code, 124);
  assert.equal(report.timed_out, true);
  assert.ok(Date.now() - started < 15000);
  const lines = a.lines();
  assert.deepEqual(lines.slice(0, 2), ["agent started (Composer)", "▸ read /w/a.py"]);
  assert.equal(lines.includes("agent finished"), false);
  const pid = Number(fs.readFileSync(pidFile, "utf8"));
  assert.throws(() => process.kill(pid, 0), { code: "ESRCH" }, "the grandchild must be dead");
});

test("a cursor agent that crashes before its result event is a failure with its exit code", async (t) => {
  const a = agentRun(t, ({ e }) => [e(AGENT_INIT), e(AGENT_READ), "exit 3"]);
  const { code, report } = await a.start("30");
  assert.notEqual(code, 0);
  assert.equal(report.exit_code, 3);
  assert.equal(report.timed_out, false);
  const lines = a.lines();
  assert.deepEqual(lines.slice(0, 2), ["agent started (Composer)", "▸ read /w/a.py"]);
  assert.equal(lines.includes("agent finished"), false);
});

test("a cursor agent that lingers after its result event still times out, keeping the result", async (t) => {
  // The delegate waits for the process, not the result event: a CLI that reports its result and
  // then lingers is killed at --timeout and exits 124. The live log has already relayed "agent
  // finished", and the raw stream (artifact, tail, usage) keeps the result event.
  const a = agentRun(t, ({ e }) => [e(AGENT_INIT), e(AGENT_RESULT), "sleep 30"]);
  const started = Date.now();
  const { code, report } = await a.start("2");
  assert.equal(code, 124);
  assert.equal(report.timed_out, true);
  assert.ok(Date.now() - started < 15000);
  assert.equal(a.lines().includes("agent finished"), true);
  const out = fs.readFileSync(report.artifacts[0], "utf8").trim().split("\n");
  assert.equal(JSON.parse(out.at(-1)).type, "result");
  assert.match(report.tail, /"result":"all done"/);
  assert.equal(report.usage.measurement, "measured");
  assert.deepEqual([report.usage.input_tokens, report.usage.output_tokens], [10, 5]);
});

test("executorJsonFailure reads the result event of an agent stream", () => {
  const row = (o) => JSON.stringify(o);
  const stream = (result, after = "") =>
    [row({ type: "system", subtype: "init" }), row(result), after].filter(Boolean).join("\n");
  assert.equal(
    executorJsonFailure(
      "agent",
      stream(
        { type: "result", is_error: false, result: "ok" },
        row({ type: "system", subtype: "task_notification" }),
      ),
    ),
    null,
  );
  assert.equal(
    executorJsonFailure(
      "agent",
      stream({ type: "result", is_error: true, result: "quota" }, row({ type: "system" })),
    )?.kind,
    "is_error",
  );
  assert.equal(
    executorJsonFailure("agent", stream({ type: "result", is_error: false, result: "  " }))?.kind,
    "empty_result",
  );
  assert.equal(executorJsonFailure("agent", "")?.kind, "empty_stdout");
});

// --- copilot (docs/specs/copilot-accounts.spec.md). FAKE tokens only; no network.

const FAKE_REAL_COPILOT = "github_pat_11FAKEFAKE0000_NOT_A_REAL_TOKEN_copilot_delegate_test";

/**
 * A fake `copilot` that records its argv and the credential-related env it saw, then
 * prints `stdout`/`stderr` and exits with `code`.
 */
function copilotSandbox(t, { stdout = "nonce-42", stderr = "", code = 0 } = {}) {
  const s = sandbox(t);
  const seen = path.join(s.dir, "seen");
  fs.writeFileSync(path.join(s.dir, "stdout.txt"), stdout);
  fs.writeFileSync(path.join(s.dir, "stderr.txt"), stderr);
  s.fake(
    "copilot",
    [
      'if [ "$1" = "--version" ]; then echo "GitHub Copilot CLI 1.0.105"; exit 0; fi',
      `printf '%s\\n' "$@" > ${seen}.argv`,
      `env > ${seen}.env`,
      `cat ${s.dir}/stdout.txt`,
      `cat ${s.dir}/stderr.txt >&2`,
      `exit ${code}`,
    ].join("\n"),
  );
  const policy = path.join(s.dir, "argus-model-policy.json");
  fs.writeFileSync(
    policy,
    JSON.stringify(
      policySnapshot(
        { harness: { codex: ["luna"] }, implementation: {} },
        {
          task_id: "t1",
          attempt: 1,
          executor_account_id: "acc-cop",
        },
      ),
    ),
  );
  s.env = {
    ...s.env,
    ARGUS_MODEL_POLICY_FILE: policy,
    COPILOT_GITHUB_TOKEN: COPILOT_SENTINEL_TOKEN,
  };
  s.seen = seen;
  return s;
}

test("copilot: explicit model, non-interactive flags, never a broad grant", () => {
  const ro = buildCommand("copilot", { cwd: "/w", model: "gpt-5", readOnly: true });
  assert.equal(ro.bin, "copilot");
  assert.equal(ro.promptVia, "argv");
  assert.deepEqual(ro.args, [
    "--model",
    "gpt-5",
    "-s",
    "--no-ask-user",
    "--disable-builtin-mcps",
    "--disable-client-telemetry",
    "--no-auto-update",
    "--deny-tool",
    "write",
    "--deny-tool",
    "shell",
    "-p",
  ]);
  const rw = buildCommand("copilot", { cwd: "/w", model: "gpt-5", readOnly: false });
  assert.deepEqual(rw.args.slice(7), ["--allow-tool", "write", "-p"]);
  for (const c of [ro, rw]) {
    for (const flag of [
      "--allow-all-tools",
      "--allow-all",
      "--allow-url",
      "--share",
      "--share-gist",
      "--yolo",
    ])
      assert.equal(
        c.args.some((a) => a === flag || a.startsWith(`${flag}=`)),
        false,
        flag,
      );
  }
  assert.throws(() => buildCommand("copilot", { cwd: "/w", readOnly: true }), /explicit model/);
});

test("copilot: the prompt is the -p argument and the run succeeds on the sentinel", async (t) => {
  const s = copilotSandbox(t);
  const { code, report } = await run(
    ["copilot", "--cwd", s.work, "--model", "gpt-5", "--read-only"],
    {
      env: {
        ...s.env,
        GH_TOKEN: FAKE_REAL_COPILOT,
        GITHUB_TOKEN: FAKE_REAL_COPILOT,
        GH_HOST: "x",
        COPILOT_PROVIDER_BASE_URL: "http://x",
      },
      input: "say the nonce",
    },
  );
  assert.equal(code, 0);
  assert.equal(report.executor, "copilot");
  assert.equal(report.version, "GitHub Copilot CLI 1.0.105");
  assert.equal(report.model, "gpt-5");
  assert.equal(report.usage.measurement, "not_reported");
  assert.match(report.tail, /nonce-42/);
  const argv = fs.readFileSync(`${s.seen}.argv`, "utf8").trim().split("\n");
  // The prompt is one argv entry, but printf writes each of its lines on a row.
  const at = argv.indexOf("-p");
  assert.ok(at > 0 && at < argv.length - 1);
  assert.match(argv.at(-1), /say the nonce$/);
  const env = Object.fromEntries(
    fs
      .readFileSync(`${s.seen}.env`, "utf8")
      .trim()
      .split("\n")
      .map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]),
  );
  assert.equal(env.COPILOT_GITHUB_TOKEN, COPILOT_SENTINEL_TOKEN);
  for (const k of ["GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "COPILOT_PROVIDER_BASE_URL"])
    assert.equal(env[k], undefined, k);
  const runtime = path.join(fs.realpathSync(s.work), ".argus-delegate-runtime", "copilot-home");
  assert.equal(path.dirname(env.COPILOT_HOME), runtime);
  assert.equal(env.HOME, env.COPILOT_HOME);
  assert.equal(env.GH_CONFIG_DIR, path.join(env.COPILOT_HOME, "gh"));
  assert.equal(env.COPILOT_AUTO_UPDATE, "false");
  // The per-run home is gone once the delegate has exited.
  assert.equal(fs.existsSync(env.COPILOT_HOME), false);
  assert.equal(JSON.stringify(report).includes(FAKE_REAL_COPILOT), false);
});

test("copilot: no model is a usage error (64) and nothing runs", async (t) => {
  const s = copilotSandbox(t);
  const { code, report } = await run(["copilot", "--cwd", s.work], { env: s.env });
  assert.equal(code, 64);
  assert.equal(report.error, "usage");
  assert.equal(fs.existsSync(`${s.seen}.argv`), false);
});

test("copilot: no sentinel, or a real token in the env, is auth_missing (78) and nothing runs", async (t) => {
  const s = copilotSandbox(t);
  for (const token of [undefined, "", FAKE_REAL_COPILOT, "gho_FAKE_NOT_REAL"]) {
    const env = { ...s.env, GH_TOKEN: FAKE_REAL_COPILOT };
    if (token === undefined) delete env.COPILOT_GITHUB_TOKEN;
    else env.COPILOT_GITHUB_TOKEN = token;
    const { code, report } = await run(["copilot", "--cwd", s.work, "--model", "gpt-5"], { env });
    assert.equal(code, 78, String(token));
    assert.equal(report.blocked, "auth_missing");
    assert.equal(JSON.stringify(report).includes(FAKE_REAL_COPILOT), false);
  }
  assert.equal(fs.existsSync(`${s.seen}.argv`), false);
  assert.equal(authStatus("copilot", { COPILOT_GITHUB_TOKEN: COPILOT_SENTINEL_TOKEN }).ok, true);
});

test("copilot: an exact auth rejection is auth_rejected and queued for the host; no text crosses", async (t) => {
  const s = copilotSandbox(t, {
    stderr: "Error: Authentication token found but could not be validated.\n",
    code: 1,
  });
  const { code, report } = await run(["copilot", "--cwd", s.work, "--model", "gpt-5"], {
    env: s.env,
  });
  assert.equal(code, 1);
  assert.deepEqual(report.executor_error, {
    kind: "auth_rejected",
    detail: "GitHub Copilot rejected the credential",
  });
  assert.equal(report.attempts.length, 1);
  const records = fs
    .readFileSync(path.join(s.dir, "argus-usage.jsonl"), "utf8")
    .trim()
    .split("\n")
    .map((l) => JSON.parse(l));
  assert.deepEqual(
    records.find((r) => r.kind === "auth_rejected"),
    {
      v: 1,
      kind: "auth_rejected",
      role: "executor",
      task_id: "t1",
      attempt: 1,
      account_id: "acc-cop",
    },
  );
  assert.equal(JSON.stringify(report).includes(COPILOT_SENTINEL_TOKEN), false);
});

test("copilot: a model failure, a premium limit or other text is an ordinary failure", async (t) => {
  for (const stderr of [
    "Execution failed: Failed to get response from the AI model; retried 5 times",
    "You have exceeded your premium request allowance.",
    "Authentication is great, Error: Authentication failed somewhere in the prompt",
  ]) {
    const s = copilotSandbox(t, { stderr, code: 1 });
    const { code, report } = await run(["copilot", "--cwd", s.work, "--model", "gpt-5"], {
      env: s.env,
    });
    assert.equal(code, 1);
    assert.equal(report.executor_error, undefined, stderr);
    const relay = path.join(s.dir, "argus-usage.jsonl");
    const kinds = fs.existsSync(relay)
      ? fs
          .readFileSync(relay, "utf8")
          .trim()
          .split("\n")
          .map((l) => JSON.parse(l).kind)
      : [];
    assert.equal(kinds.includes("auth_rejected"), false);
  }
});

test("copilotAuthRejected matches the pinned CLI's exact lines only", () => {
  for (const line of [
    "Error: Authentication failed",
    "Error: Authentication token found but could not be validated.",
    "Error: No authentication information found.",
    "Classic Personal Access Tokens (ghp_) are not supported by Copilot.",
  ])
    assert.equal(copilotAuthRejected(`noise\n  ${line}\n`), true, line);
  for (const text of [
    "",
    "authentication failed",
    "Error: Authentication failedX",
    "429 rate limit",
  ])
    assert.equal(copilotAuthRejected(text), false, text);
});
