import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  authorizedCwd,
  authStatus,
  buildCommand,
  executorJsonFailure,
  READ_ONLY_PROMPT_PREFIX,
} from "./argus-delegate.mjs";

const DELEGATE = path.join(path.dirname(fileURLToPath(import.meta.url)), "argus-delegate.mjs");

/** A sandbox: a workspace root, a fake HOME, and fake executables first on PATH. */
function sandbox(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "delegate-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
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
  assert.equal(authStatus("agent", { CURSOR_API_KEY: "x" }, "/nonexistent").ok, true);
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
  const { code, report } = await run(["agent", "--cwd", s.work], {
    env: { ...s.env, PATH: s.bin, CURSOR_API_KEY: "x" },
  });
  assert.equal(code, 127);
  assert.equal(report.blocked, "not_installed");
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
  assert.ok(claudeWrite.args.includes("acceptEdits"));
  assert.ok(claudeWrite.args.includes("--allowed-tools"));
  assert.ok(claudeWrite.args.join(" ").includes("Bash"));
  const agentWrite = buildCommand("agent", { cwd: "/w", readOnly: false });
  assert.ok(agentWrite.args.includes("--force"));
  assert.equal(agentWrite.args.includes("--mode"), false);
  const agentRo = buildCommand("agent", { cwd: "/w", readOnly: true });
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
  s.fake(
    "agent",
    'if [ "$1" = "--version" ]; then echo v; exit 0; fi\necho \'{"type":"result","result":""}\'',
  );
  const { code, report } = await run(["agent", "--cwd", s.work], {
    env: { ...s.env, CURSOR_API_KEY: "x" },
  });
  assert.equal(code, 1);
  assert.equal(report.executor_error?.kind, "empty_result");
});

test("read-only agent uses print only with a static-review prompt prefix", async (t) => {
  const s = sandbox(t);
  const got = path.join(s.dir, "prompt.txt");
  s.fake(
    "agent",
    `if [ "$1" = "--version" ]; then echo v; exit 0; fi\necho "$@" >${got}; echo '{"type":"result","result":"ok"}'`,
  );
  await run(["agent", "--cwd", s.work, "--read-only"], {
    env: { ...s.env, CURSOR_API_KEY: "x" },
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
  s.fake("claude", `if [ "$1" = "--version" ]; then echo v; exit 0; fi
cat >/dev/null
case "$*" in *sonnet*) echo '{"is_error":true,"result":"context exceeded"}';; *) echo '{"result":"ok"}';; esac`);
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
  s.fake("claude", `if [ "$1" = "--version" ]; then echo v; exit 0; fi
cat >/dev/null; echo '{"is_error":true,"result":"failed"}'`);
  const pinned = await run(["claude", "--cwd", s.work, "--model", "custom"], {
    env: { ...s.env, ANTHROPIC_API_KEY: "x" },
  });
  assert.equal(pinned.code, 1);
  assert.equal(pinned.report.attempts.length, 1);
  assert.equal(pinned.report.requested_model, "custom");
  const { policySnapshot } = await import("./model-policy.mjs");
  const snapshot = policySnapshot({ harness: {}, implementation: { claude: ["sonnet", "opus"] } },
    { task_id: "task1", attempt: 3 });
  const file = path.join(s.work, "policy.json");
  fs.writeFileSync(file, JSON.stringify(snapshot));
  const retry = await run(["claude", "--cwd", s.work, "--model-attempt", "2", "--fallback-reason", "verification_failed"], {
    env: { ...s.env, ANTHROPIC_API_KEY: "x", ARGUS_MODEL_POLICY_FILE: file },
  });
  assert.equal(retry.report.configured_model, "opus");
  assert.equal(retry.report.fallback_reason, "verification_failed");
  assert.deepEqual(retry.report.task, { task_id: "task1", attempt: 3 });
  assert.equal(retry.report.attempts.length, 1);
});

test("invalid policies and invalid retry requests fail before CLI execution", async (t) => {
  const s = sandbox(t);
  const marker = path.join(s.dir, "ran");
  s.fake("claude", `touch ${marker}; exit 0`);
  const env = { ...s.env, ANTHROPIC_API_KEY: "x" };
  for (const args of [["--model-attempt", "0"], ["--model-attempt", "2"], ["--model-attempt", "NaN"]]) {
    assert.equal((await run(["claude", "--cwd", s.work, ...args], { env })).code, 64);
  }
  assert.equal((await run(["claude", "--cwd", s.work], { env: { ...env, ARGUS_MODEL_POLICY: "{}" } })).code, 64);
  assert.equal(fs.existsSync(marker), false);
});
