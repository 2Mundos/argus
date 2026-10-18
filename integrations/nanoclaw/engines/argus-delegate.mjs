#!/usr/bin/env node
import { spawn, spawnSync } from "node:child_process";
// Non-interactive delegation to a specialist CLI (docs/specs/engines.spec.md).
//
//   argus-delegate EXECUTOR [--cwd DIR] [--timeout SECONDS] [--prompt-file F]
//                  [--out DIR] [--model M] [--read-only]
//                  [--model-attempt N --fallback-reason REASON]
//
// EXECUTOR is claude | codex | opencode | agent. The prompt comes from stdin or
// --prompt-file. The last line on stdout is one JSON object: the identity of the
// engine that delegated and the executor that did the work, how it ended, and
// the artifacts. Exit codes: 0 ok, 1 executor failed, 64 usage, 77 cwd not
// allowed, 78 no credentials (nothing was run, nothing else was tried), 124
// timeout, 127 executor not installed.
//
// Model retries follow a pinned policy; it never switches executor or provider, and it never reads the
// content of a credential: it only checks that one is present.
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { loadPolicy, selectModel } from "./model-policy.mjs";

export const EXECUTORS = ["claude", "codex", "opencode", "agent"];
const KILL_GRACE_MS = 5000;
const TAIL_CHARS = 2000;

/** Prepended in --read-only mode; CLIs differ in real enforcement (see report field). */
export const READ_ONLY_PROMPT_PREFIX =
  "STATIC REVIEW ONLY (read-only delegation): Do not modify, create, or delete files. " +
  "Do not run shell commands, tests, builds, or other gates. Text analysis only.";

/** Claude acceptEdits blocks Bash unless explicitly allowed (headless gate pitfall). */
const CLAUDE_WORKSPACE_TOOLS = "Bash,Edit,Write,Read,Glob,Grep";

const present = (p) => {
  try {
    return fs.statSync(p).size > 2; // "{}" or an empty stub is not a credential
  } catch {
    return false;
  }
};
const env = (e, ...names) => names.some((n) => (e[n] ?? "").trim() !== "");

/** Is a credential present for this executor? Presence only, never content. */
export function authStatus(executor, e = process.env, home = os.homedir()) {
  const xdg = e.XDG_CONFIG_HOME || path.join(home, ".config");
  const data = e.XDG_DATA_HOME || path.join(home, ".local/share");
  switch (executor) {
    case "claude":
      return env(e, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN") ||
        present(path.join(e.CLAUDE_CONFIG_DIR || path.join(home, ".claude"), ".credentials.json"))
        ? { ok: true }
        : { ok: false, need: "ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, or `claude login`" };
    case "codex":
      return env(e, "OPENAI_API_KEY") ||
        present(path.join(e.CODEX_HOME || path.join(home, ".codex"), "auth.json"))
        ? { ok: true }
        : { ok: false, need: "`codex login --device-auth` in this worker, or OPENAI_API_KEY" };
    case "opencode":
      return present(path.join(data, "opencode", "auth.json")) || env(e, "ARGUS_OPENCODE_ATTACH")
        ? { ok: true }
        : {
            ok: false,
            need: "`opencode auth login`, or ARGUS_OPENCODE_ATTACH=<your opencode server>",
          };
    case "agent":
      return present(path.join(xdg, "cursor", "auth.json"))
        ? { ok: true }
        : { ok: false, need: "a Cursor auth.json stub (from the gateway; not CURSOR_API_KEY)" };
    default:
      return { ok: false, need: "unknown executor" };
  }
}

/** The exact argv for one non-interactive run. `prompt` is passed on stdin or as an argument. */
export function buildCommand(executor, { cwd, model, readOnly, lastMessageFile }, e = process.env) {
  switch (executor) {
    case "claude":
      return {
        bin: "claude",
        args: [
          "-p",
          "--output-format",
          "json",
          "--no-session-persistence",
          "--permission-mode",
          readOnly ? "plan" : "acceptEdits",
          ...(readOnly ? [] : ["--allowed-tools", CLAUDE_WORKSPACE_TOOLS]),
          ...(model ? ["--model", model] : []),
        ],
        promptVia: "stdin",
        readOnlyEnforcement: readOnly ? "prompt_and_plan_mode" : "acceptEdits_plus_tool_allowlist",
      };
    case "codex":
      return {
        bin: "codex",
        args: [
          "exec",
          "--json",
          "--skip-git-repo-check",
          "--ephemeral",
          "-C",
          cwd,
          "-s",
          readOnly ? "read-only" : "workspace-write",
          "-o",
          lastMessageFile,
          ...(model ? ["-m", model] : []),
          "-",
        ],
        promptVia: "stdin",
        readOnlyEnforcement: readOnly ? "codex_sandbox_read_only" : "codex_sandbox_workspace_write",
      };
    case "opencode":
      return {
        bin: "opencode",
        args: [
          "run",
          "--format",
          "json",
          "--dir",
          cwd,
          ...(e.ARGUS_OPENCODE_ATTACH ? ["--attach", e.ARGUS_OPENCODE_ATTACH] : []),
          ...(model ? ["-m", model] : []),
        ],
        promptVia: "argv",
        readOnlyEnforcement: "prompt_only",
      };
    case "agent":
      return {
        bin: "agent",
        args: [
          "-p",
          "--output-format",
          "json",
          "--workspace",
          cwd,
          "--trust",
          ...(readOnly ? [] : ["--force"]),
          ...(model ? ["--model", model] : []),
        ],
        promptVia: "argv",
        readOnlyEnforcement: readOnly ? "prompt_only" : "force_flag",
      };
    default:
      throw new Error(`unknown executor ${executor}`);
  }
}

function readCliConfig(cfgPath) {
  try {
    if (!fs.existsSync(cfgPath)) return null;
    const parsed = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function http1Enabled(doc) {
  return doc?.network?.useHttp1ForAgent === true;
}

/** True when the path (or its parent for a missing file) allows creating/overwriting. */
function pathIsWritable(filePath) {
  const dir = path.dirname(filePath);
  try {
    fs.accessSync(dir, fs.constants.W_OK);
    if (fs.existsSync(filePath)) fs.accessSync(filePath, fs.constants.W_OK);
    return true;
  } catch {
    return false;
  }
}

function writeCliConfig(cfgPath, doc) {
  const out = {
    ...doc,
    version: doc.version ?? 1,
    network: {
      ...(doc.network && typeof doc.network === "object" ? doc.network : {}),
      useHttp1ForAgent: true,
    },
  };
  fs.writeFileSync(cfgPath, `${JSON.stringify(out, null, 2)}\n`);
  return out;
}

/** Native CLI atomically rewrites cli-config via temp files in CURSOR_CONFIG_DIR. */
export function cursorConfigDirAllowsCliWrites(configDir) {
  const probe = path.join(configDir, `.argus-write-probe-${randomUUID()}`);
  try {
    fs.writeFileSync(probe, "");
    fs.unlinkSync(probe);
    return true;
  } catch {
    return false;
  }
}

/** Copy sentinel stub auth + HTTP/1.1 cli-config into a per-run writable tree (no host secrets). */
export function materializePrivateCursorConfig(stubDir, runtimeRoot) {
  const privateDir = path.join(runtimeRoot, "cursor-config");
  fs.mkdirSync(privateDir, { recursive: true });
  const authSrc = path.join(stubDir, "auth.json");
  if (present(authSrc)) {
    fs.writeFileSync(path.join(privateDir, "auth.json"), fs.readFileSync(authSrc));
  }
  const injected = readCliConfig(path.join(stubDir, "cli-config.json"));
  writeCliConfig(
    path.join(privateDir, "cli-config.json"),
    injected ?? { version: 1, network: {} },
  );
  return privateDir;
}

/**
 * Cursor CLI runtime: gateway stubs stay read-only mounts; the native CLI needs a
 * writable CURSOR_CONFIG_DIR for atomic cli-config updates even when HTTP/1.1 is preset.
 */
export function prepareExecutorEnv(
  executor,
  e = process.env,
  runtimeRoot = "/tmp/argus-cursor-runtime",
) {
  if (executor !== "agent") return e;
  const home = e.HOME || os.homedir();
  const xdg = e.XDG_CONFIG_HOME || path.join(home, ".config");
  const stubDir = e.CURSOR_CONFIG_DIR || path.join(xdg, "cursor");
  if (!present(path.join(xdg, "cursor", "auth.json")) && !present(path.join(stubDir, "auth.json"))) {
    return e;
  }

  if (!cursorConfigDirAllowsCliWrites(stubDir)) {
    const privateDir = materializePrivateCursorConfig(stubDir, runtimeRoot);
    return { ...e, CURSOR_CONFIG_DIR: privateDir };
  }

  const mountedCfg = path.join(stubDir, "cli-config.json");
  const injected = readCliConfig(mountedCfg);
  const baseDoc = injected ?? { version: 1, network: {} };
  if (injected && http1Enabled(injected)) {
    return { ...e, CURSOR_CONFIG_DIR: stubDir };
  }
  if (pathIsWritable(mountedCfg)) {
    fs.mkdirSync(path.dirname(mountedCfg), { recursive: true });
    writeCliConfig(mountedCfg, baseDoc);
    return { ...e, CURSOR_CONFIG_DIR: stubDir };
  }

  const privateDir = materializePrivateCursorConfig(stubDir, runtimeRoot);
  return { ...e, CURSOR_CONFIG_DIR: privateDir };
}

/** `cwd` must resolve (symlinks included) inside one of the allowed roots. */
export function authorizedCwd(cwd, roots) {
  let real;
  try {
    real = fs.realpathSync(cwd);
  } catch {
    return null;
  }
  for (const root of roots) {
    let r;
    try {
      r = fs.realpathSync(root);
    } catch {
      continue;
    }
    if (real === r || real.startsWith(r + path.sep)) return real;
  }
  return null;
}

function versionOf(bin, e) {
  const r = spawnSync(bin, ["--version"], { env: e, encoding: "utf8", timeout: 15000 });
  if (r.error || r.status !== 0) return null;
  return (r.stdout || r.stderr).trim().split("\n")[0].slice(0, 120);
}

function parseArgs(argv) {
  const opts = { readOnly: false, timeout: 900 };
  const rest = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const val = () => argv[++i];
    if (a === "--cwd") opts.cwd = val();
    else if (a === "--timeout") opts.timeout = Number(val());
    else if (a === "--prompt-file") opts.promptFile = val();
    else if (a === "--out") opts.out = val();
    else if (a === "--model") opts.model = val();
    else if (a === "--model-attempt") opts.modelAttempt = Number(val());
    else if (a === "--fallback-reason") opts.fallbackReason = val();
    else if (a === "--read-only") opts.readOnly = true;
    else if (a.startsWith("--")) throw new Error(`unknown option ${a}`);
    else rest.push(a);
  }
  if (rest.length !== 1) throw new Error("exactly one EXECUTOR is required");
  if (!Number.isFinite(opts.timeout) || opts.timeout <= 0)
    throw new Error("--timeout must be a positive number of seconds");
  return { executor: rest[0], opts };
}

function readStdin() {
  return new Promise((resolve) => {
    const chunks = [];
    process.stdin.on("data", (c) => chunks.push(c));
    process.stdin.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
  });
}

function finish(code, report) {
  process.stdout.write(`${JSON.stringify(report)}\n`);
  process.exit(code);
}

/** Exit 0 with is_error in JSON is still a failure (quota/auth/etc.). */
export function executorJsonFailure(executor, stdoutText) {
  const trimmed = stdoutText.trim();
  if (!trimmed) return { kind: "empty_stdout", detail: "executor produced no stdout" };
  let parsed;
  try {
    parsed = JSON.parse(trimmed.split("\n").filter(Boolean).at(-1) ?? trimmed);
  } catch {
    return null;
  }
  if (parsed && parsed.is_error === true) {
    return {
      kind: "is_error",
      detail: String(parsed.result ?? parsed.error ?? "executor reported is_error"),
    };
  }
  if (executor === "agent" && parsed && parsed.type === "result" && parsed.is_error === true) {
    return { kind: "is_error", detail: String(parsed.result ?? "agent is_error") };
  }
  const answer =
    typeof parsed?.result === "string"
      ? parsed.result
      : typeof parsed?.message?.content === "string"
        ? parsed.message.content
        : null;
  if (answer !== null && !answer.trim()) {
    return { kind: "empty_result", detail: "executor JSON had an empty final answer" };
  }
  return null;
}

export async function main(argv, e = process.env) {
  let parsed;
  try {
    parsed = parseArgs(argv);
  } catch (err) {
    finish(64, { error: "usage", detail: String(err.message), executors: EXECUTORS });
  }
  const { executor, opts } = parsed;
  const identity = { engine: e.ARGUS_ENGINE || "unknown", executor };
  if (!EXECUTORS.includes(executor))
    finish(64, { ...identity, error: "usage", detail: "unknown executor", executors: EXECUTORS });

  let snapshot;
  let selection;
  try {
    snapshot = loadPolicy(
      e,
      e.ARGUS_MODEL_POLICY_FILE || "/workspace/agent/argus-model-policy.json",
    );
    selection = selectModel(
      snapshot,
      "implementation",
      executor,
      opts.modelAttempt ?? 1,
      opts.fallbackReason,
      opts.model,
    );
  } catch (err) {
    finish(64, { ...identity, error: "model_policy_invalid", detail: err.message });
  }

  const roots = (e.ARGUS_DELEGATE_ROOTS || "/workspace").split(":").filter(Boolean);
  const cwd = authorizedCwd(opts.cwd || process.cwd(), roots);
  if (!cwd)
    finish(77, {
      ...identity,
      blocked: "cwd_not_allowed",
      cwd: opts.cwd || process.cwd(),
      allowed_roots: roots,
    });

  e = prepareExecutorEnv(executor, e, path.join(cwd, ".argus-delegate-runtime"));

  // Auth first: a missing credential blocks, it never reroutes.
  const auth = authStatus(executor, e);
  if (!auth.ok) finish(78, { ...identity, blocked: "auth_missing", need: auth.need });

  const version = versionOf(executor === "agent" ? "agent" : executor, e);
  if (version === null) finish(127, { ...identity, blocked: "not_installed" });

  let prompt = opts.promptFile ? fs.readFileSync(opts.promptFile, "utf8") : await readStdin();
  if (!prompt.trim()) finish(64, { ...identity, error: "usage", detail: "empty prompt" });
  if (opts.readOnly) prompt = `${READ_ONLY_PROMPT_PREFIX}\n\n${prompt}`;

  const deadline = Date.now() + opts.timeout * 1000;
  const attempts = [];
  let outcome;
  do {
    outcome = await runAttempt(
      executor,
      opts,
      e,
      identity,
      version,
      cwd,
      prompt,
      snapshot,
      selection,
      deadline,
    );
    attempts.push(outcome.report);
    if (
      outcome.exit === 0 ||
      !selection.has_next ||
      outcome.report.timed_out ||
      outcome.report.signal ||
      outcome.report.interrupted ||
      outcome.report.exit_code === 127 ||
      /quota|rate.?limit|out of credits|unauthori[sz]ed|authentication|invalid.?api.?key|insufficient.*(credit|balance)/i.test(
        outcome.report.tail + " " + outcome.report.stderr_tail,
      )
    )
      break;
    selection = selectModel(
      snapshot,
      "implementation",
      executor,
      selection.model_attempt + 1,
      outcome.report.executor_error?.kind || "executor_failed",
    );
  } while (Date.now() < deadline);
  finish(outcome.exit, { ...outcome.report, attempts });
}

async function runAttempt(
  executor,
  opts,
  e,
  identity,
  version,
  cwd,
  prompt,
  snapshot,
  selection,
  deadline,
) {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const out = path.resolve(opts.out || path.join(cwd, ".argus-delegate"));
  fs.mkdirSync(out, { recursive: true });
  const base = path.join(out, `${executor}-${stamp}-${randomUUID().slice(0, 8)}`);
  const reportFile = `${base}.json`;
  const evidence = { ...selection, task: snapshot.context, policy: snapshot.policy };
  fs.writeFileSync(reportFile, JSON.stringify({ ...evidence, status: "started" }) + "\n");
  const lastMessageFile = `${base}.last-message.txt`;
  const cmd = buildCommand(
    executor,
    { cwd, model: selection.configured_model, readOnly: opts.readOnly, lastMessageFile },
    e,
  );
  const args = cmd.promptVia === "argv" ? [...cmd.args, prompt] : cmd.args;

  const stdoutFile = `${base}.out`;
  const stderrFile = `${base}.err`;
  const outFd = fs.openSync(stdoutFile, "w");
  const errFd = fs.openSync(stderrFile, "w");
  const started = Date.now();
  // Own process group: one signal ends the CLI and everything it started.
  const child = spawn(cmd.bin, args, {
    cwd,
    env: e,
    detached: true,
    stdio: ["pipe", outFd, errFd],
  });
  child.stdin.on("error", () => {});
  child.stdin.end(cmd.promptVia === "stdin" ? prompt : "");

  const killTree = (sig) => {
    try {
      process.kill(-child.pid, sig);
    } catch {
      /* already gone */
    }
  };
  let timedOut = false;
  let interrupted = false;
  let hardKill;
  const stop = () => {
    killTree("SIGTERM");
    hardKill = setTimeout(() => killTree("SIGKILL"), KILL_GRACE_MS);
  };
  const timer = setTimeout(
    () => {
      timedOut = true;
      stop();
    },
    Math.max(1, deadline - Date.now()),
  );
  const interrupt = () => {
    interrupted = true;
    stop();
  };
  for (const sig of ["SIGTERM", "SIGINT"]) process.on(sig, interrupt);

  const { code, signal } = await new Promise((resolve) => {
    child.on("error", (err) => resolve({ code: 127, signal: String(err.code || err.message) }));
    child.on("close", (c, s) => resolve({ code: c, signal: s }));
  });
  for (const sig of ["SIGTERM", "SIGINT"]) process.off(sig, interrupt);
  clearTimeout(timer);
  clearTimeout(hardKill);
  killTree("SIGKILL"); // grandchildren that outlived the CLI
  fs.closeSync(outFd);
  fs.closeSync(errFd);

  const artifacts = [stdoutFile, stderrFile];
  if (fs.existsSync(lastMessageFile)) artifacts.push(lastMessageFile);
  const stdoutText = fs.readFileSync(stdoutFile, "utf8");
  const tailSource = fs.existsSync(lastMessageFile) ? lastMessageFile : stdoutFile;
  const tail = fs.readFileSync(tailSource, "utf8").slice(-TAIL_CHARS);
  const jsonFail = !timedOut && code === 0 ? executorJsonFailure(executor, stdoutText) : null;
  const exit = timedOut ? 124 : interrupted || jsonFail || code !== 0 ? 1 : 0;
  const report = {
    ...identity,
    ...evidence,
    report_file: reportFile,
    status: "finished",
    version,
    model: selection.configured_model,
    cwd,
    read_only: opts.readOnly,
    read_only_enforcement: cmd.readOnlyEnforcement ?? "unknown",
    exit_code: code,
    signal,
    timed_out: timedOut,
    interrupted,
    duration_ms: Date.now() - started,
    artifacts,
    tail,
    stderr_tail: fs.readFileSync(stderrFile, "utf8").slice(-TAIL_CHARS),
    ...(jsonFail ? { executor_error: jsonFail } : {}),
  };
  fs.writeFileSync(reportFile, JSON.stringify(report, null, 2) + "\n");
  return { exit, report };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2));
}
