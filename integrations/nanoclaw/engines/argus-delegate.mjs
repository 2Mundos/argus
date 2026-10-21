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
// the artifacts, and the run's token usage (`usage`, `run_key`, `usage_report`; see
// usage.mjs). Exit codes: 0 ok, 1 executor failed, 64 usage, 77 cwd not
// allowed, 78 no credentials (nothing was run, nothing else was tried), 124
// timeout, 127 executor not installed.
//
// Model retries follow a pinned policy; it never switches executor or provider, and it never reads the
// content of a credential: it only checks that one is present.
import { randomUUID } from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import {
  CUSTOM_KEY_ENV,
  CUSTOM_PROVIDER_ID,
  parseAccountSettings,
  settingsSupport,
} from "./account-settings.mjs";
import { anthropicRedirectRejection, LOCAL_HARNESS_AUTH_TOKEN } from "./anthropic-redirect.mjs";
import { createFileRelay, envSecrets, RELAY_FILE, startLiveLog } from "./livelog.mjs";
import { createSink, createTracker, gitHead, waitingInput } from "./milestones.mjs";
import { implementationScope, loadPolicy, selectModel } from "./model-policy.mjs";
import {
  appendUsageRecord,
  parseUsage,
  runKey,
  runKeyHash,
  USAGE_RELAY_FILE,
} from "./usage.mjs";

export const EXECUTORS = ["claude", "codex", "opencode", "agent"];
const KILL_GRACE_MS = 5000;
const TAIL_CHARS = 2000;
const OUTPUT_SCAN_BYTES = 1_000_000;

const readTail = (file) => {
  try {
    const size = fs.statSync(file).size;
    const fd = fs.openSync(file, "r");
    try {
      const buf = Buffer.alloc(Math.min(size, OUTPUT_SCAN_BYTES));
      fs.readSync(fd, buf, 0, buf.length, size - buf.length);
      return buf.toString("utf8");
    } finally {
      fs.closeSync(fd);
    }
  } catch {
    return "";
  }
};

/** CI statuses for a commit through the credential-free Gitea client, or undefined when not configured. */
export async function giteaCiProbe(e) {
  const repository = (e.ARGUS_REPOSITORY_URL ?? "").trim();
  if (!repository || !e.ARGUS_REPOSITORIES_JSON) return undefined;
  try {
    const here = path.dirname(fileURLToPath(import.meta.url));
    const mod = await import(
      pathToFileURL(
        [path.join(here, "gitea-api.mjs"), path.join(here, "..", "gitea-api.mjs")].find((f) =>
          fs.existsSync(f),
        ),
      ).href
    );
    const api = new mod.GiteaApi(repository, Object.values(JSON.parse(e.ARGUS_REPOSITORIES_JSON)));
    return (sha) => api.request(`/statuses/${encodeURIComponent(sha)}`);
  } catch {
    return undefined;
  }
}
const USAGE_POST_TIMEOUT_MS = 5000;

/** Prepended in --read-only mode; CLIs differ in real enforcement (see report field). */
export const READ_ONLY_PROMPT_PREFIX =
  "STATIC REVIEW ONLY (read-only delegation): Do not modify, create, or delete files. " +
  "Do not run shell commands, tests, builds, or other gates. Text analysis only.";

/** Claude acceptEdits blocks Bash unless explicitly allowed (headless gate pitfall). */
const CLAUDE_WORKSPACE_TOOLS = "Bash,Edit,Write,Read,Glob,Grep";

export { CUSTOM_KEY_ENV, CUSTOM_PROVIDER_ID, parseAccountSettings, settingsSupport };

/**
 * Model precedence: explicit --model, the task's pin, the account's model, the policy
 * chain, the native default (null). Returns the model and where it came from.
 */
export function effectiveModel({ override, taskModel, settings, policyModel }) {
  if (override) return { model: override, source: "cli" };
  if (taskModel) return { model: taskModel, source: "task" };
  if (settings?.model) return { model: settings.model, source: "account" };
  if (policyModel) return { model: policyModel, source: "policy" };
  return { model: null, source: "native_default" };
}

const present = (p) => {
  try {
    return fs.statSync(p).size > 2; // "{}" or an empty stub is not a credential
  } catch {
    return false;
  }
};
const env = (e, ...names) => names.some((n) => (e[n] ?? "").trim() !== "");

/** The model the pinned task names for this executor (task models spec), if any. */
export function pinnedModel(snapshot, executor) {
  const pin = snapshot?.context?.executor_model;
  return pin?.executor === executor ? pin.model : undefined;
}

/** Is a credential present for this executor? Presence only, never content. */
export function authStatus(executor, e = process.env, home = os.homedir(), settings = null) {
  if (settings && settingsSupport(executor).length) {
    return env(e, CUSTOM_KEY_ENV)
      ? { ok: true }
      : { ok: false, need: `${CUSTOM_KEY_ENV} (the custom account's key, from the lease)` };
  }
  const xdg = e.XDG_CONFIG_HOME || path.join(home, ".config");
  const data = e.XDG_DATA_HOME || path.join(home, ".local/share");
  switch (executor) {
    case "claude": {
      const redirectReject = anthropicRedirectRejection(
        e,
        e.ARGUS_ALLOWED_ANTHROPIC_BASE_URL ?? "",
      );
      if (redirectReject) {
        return { ok: false, need: `Anthropic API redirect not allowed (${redirectReject})` };
      }
      return env(e, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN") ||
        present(path.join(e.CLAUDE_CONFIG_DIR || path.join(home, ".claude"), ".credentials.json"))
        ? { ok: true }
        : { ok: false, need: "ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, or `claude login`" };
    }
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

/**
 * Is this the harness of a local lease (Strata)? The local model serves only the
 * NanoClaw harness: a Claude Code executor started here would inherit its
 * ANTHROPIC_BASE_URL and run the implementation on the small local context.
 */
export function localHarnessRefusal(executor, e = process.env) {
  if (executor !== "claude") return null;
  const local =
    e.ARGUS_LOCAL_HARNESS === "1" ||
    ((e.ANTHROPIC_BASE_URL ?? "").trim() !== "" &&
      (e.ANTHROPIC_AUTH_TOKEN ?? "").trim() === LOCAL_HARNESS_AUTH_TOKEN);
  return local
    ? "the local model serves only the harness; delegate with EXECUTOR agent (Cursor) or codex"
    : null;
}

/** Where the trusted host mounts the gateway stub (argus-claude-stub.ts); never configurable. */
export const CLAUDE_GATEWAY_STUB = "/home/node/.config/argus/claude-gateway.json";
const CLAUDE_OAUTH = "CLAUDE_CODE_OAUTH_TOKEN";
const SENTINEL_MARKER = /placeholder|sentinel|managed|gateway|onecli/i;

/** Can `v` be a gateway placeholder (not shaped like a real credential)? Mirrors argus-claude-stub.ts. */
export function isGatewaySentinel(v) {
  if (typeof v !== "string" || v.length < 8 || v.length > 256) return false;
  if (!/^[A-Za-z0-9._~+/=-]+$/.test(v)) return false;
  if (v.startsWith("sk-ant-") && !SENTINEL_MARKER.test(v)) return false;
  if (v.length > 120 && !SENTINEL_MARKER.test(v)) return false;
  return true;
}

const proxyAddress = (raw) => {
  try {
    const u = new URL(raw);
    if ((u.protocol !== "http:" && u.protocol !== "https:") || !u.hostname) return null;
    return `${u.hostname.toLowerCase()}:${u.port || (u.protocol === "https:" ? "443" : "80")}`;
  } catch {
    return null;
  }
};

/**
 * Claude Code removes CLAUDE_CODE_OAUTH_TOKEN from every process its Bash tool starts,
 * so a delegate started from there sees no credential even though the gateway
 * serves the worker an Anthropic account. When (and only when) the trusted host
 * mounted its stub, and the process runs through that very gateway, restore the
 * sentinel for the `claude` child. Everything is checked; any doubt keeps the
 * environment as it is and `authStatus` keeps failing closed. Returns the
 * environment, how it got its credential, and (when a stub exists but was not
 * used) a reason code that never contains a value.
 */
export function restoreClaudeGatewayAuth(e, stubPath = CLAUDE_GATEWAY_STUB) {
  const redirectReject = anthropicRedirectRejection(e, e.ARGUS_ALLOWED_ANTHROPIC_BASE_URL ?? "");
  if (redirectReject) return { env: e, source: null, stub: `rejected:${redirectReject}` };
  if ((e.ANTHROPIC_BASE_URL ?? "").trim()) return { env: e, source: "local_harness" };
  if (env(e, "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", CLAUDE_OAUTH))
    return { env: e, source: "env" };
  let st;
  try {
    st = fs.lstatSync(stubPath);
  } catch {
    return { env: e, source: null, stub: "absent" };
  }
  const reject = (reason) => ({ env: e, source: null, stub: `rejected:${reason}` });
  if (!st.isFile() || st.size > 4096) return reject("not_a_small_regular_file");
  if (st.mode & 0o022) return reject("writable_by_others");
  let doc;
  try {
    doc = JSON.parse(fs.readFileSync(stubPath, "utf8"));
  } catch {
    return reject("malformed");
  }
  const keys = ["version", "provider", "variable", "sentinel", "proxy", "ca"];
  if (
    !doc ||
    typeof doc !== "object" ||
    Array.isArray(doc) ||
    Object.keys(doc).sort().join() !== [...keys].sort().join() ||
    doc.version !== 1 ||
    doc.provider !== "anthropic" ||
    doc.variable !== CLAUDE_OAUTH
  )
    return reject("malformed");
  if (!isGatewaySentinel(doc.sentinel)) return reject("not_a_sentinel");
  // The sentinel is only ever sent to the gateway the host recorded (never a local redirect).
  if (anthropicRedirectRejection(e, "") === "api_redirected") return reject("api_redirected");
  const proxy = proxyAddress(e.HTTPS_PROXY ?? e.https_proxy ?? "");
  if (!proxy || proxy !== doc.proxy) return reject("proxy_mismatch");
  if (typeof doc.ca !== "string" || !path.isAbsolute(doc.ca) || !present(doc.ca))
    return reject("ca_missing");
  const trusted = [e.NODE_EXTRA_CA_CERTS, e.SSL_CERT_FILE].filter(Boolean);
  if (!trusted.some((f) => present(f))) return reject("ca_not_in_environment");
  return { env: { ...e, [CLAUDE_OAUTH]: doc.sentinel }, source: "gateway_sentinel" };
}

/** Codex `-c` overrides for a custom provider; the key is named by env var, never passed. */
function codexProviderArgs(settings) {
  const q = (v) => JSON.stringify(v);
  const k = `model_providers.${CUSTOM_PROVIDER_ID}`;
  return [
    "-c",
    `model_provider=${q(CUSTOM_PROVIDER_ID)}`,
    "-c",
    `${k}.name=${q(settings.label || CUSTOM_PROVIDER_ID)}`,
    "-c",
    `${k}.base_url=${q(settings.base_url)}`,
    "-c",
    `${k}.env_key=${q(CUSTOM_KEY_ENV)}`,
  ];
}

/**
 * The environment for an executor on a custom account. OpenCode gets an inline config
 * (OPENCODE_CONFIG_CONTENT) whose key is the reference `{env:ARGUS_ACCOUNT_API_KEY}`, so the
 * value is neither in argv nor in a file. Codex has no equivalent for the body `provider`.
 */
export function applySettingsEnv(executor, e, settings, model) {
  if (!settings || executor !== "opencode") return e;
  const entry = {
    npm: "@ai-sdk/openai-compatible",
    name: settings.label || CUSTOM_PROVIDER_ID,
    options: { baseURL: settings.base_url, apiKey: `{env:${CUSTOM_KEY_ENV}}` },
    models: model
      ? { [model]: settings.provider ? { options: { provider: settings.provider } } : {} }
      : {},
  };
  return {
    ...e,
    OPENCODE_CONFIG_CONTENT: JSON.stringify({
      $schema: "https://opencode.ai/config.json",
      provider: { [CUSTOM_PROVIDER_ID]: entry },
    }),
  };
}

/** The exact argv for one non-interactive run. `prompt` is passed on stdin or as an argument. */
export function buildCommand(
  executor,
  { cwd, model, readOnly, lastMessageFile, settings = null },
  e = process.env,
) {
  switch (executor) {
    case "claude":
      return {
        bin: "claude",
        args: [
          "-p",
          "--output-format",
          "stream-json",
          "--verbose",
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
          ...(settings ? codexProviderArgs(settings) : []),
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
          ...(model ? ["-m", settings ? `${CUSTOM_PROVIDER_ID}/${model}` : model] : []),
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
  writeCliConfig(path.join(privateDir, "cli-config.json"), injected ?? { version: 1, network: {} });
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
  if (
    !present(path.join(xdg, "cursor", "auth.json")) &&
    !present(path.join(stubDir, "auth.json"))
  ) {
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

/** Add the image's CLI-tool dir (normally /pnpm) to PATH before resolving executors. */
export function withCliToolsPath(e = process.env, dirs = ["/pnpm"]) {
  const parts = (e.PATH || "").split(":").filter(Boolean);
  const present = dirs.filter((d) => fs.existsSync(d) && fs.readdirSync(d).length > 0);
  const missing = present.filter((d) => !parts.includes(d));
  return missing.length ? { ...e, PATH: [...missing, ...parts].join(":") } : e;
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

export async function main(argv, e = process.env, { claudeStub = CLAUDE_GATEWAY_STUB } = {}) {
  let parsed;
  try {
    parsed = parseArgs(argv);
  } catch (err) {
    finish(64, { error: "usage", detail: String(err.message), executors: EXECUTORS });
  }
  const { executor, opts } = parsed;
  let identity = { engine: e.ARGUS_ENGINE || "unknown", executor };
  if (!EXECUTORS.includes(executor))
    finish(64, { ...identity, error: "usage", detail: "unknown executor", executors: EXECUTORS });

  let snapshot;
  let selection;
  let settings = null;
  try {
    snapshot = loadPolicy(
      e,
      e.ARGUS_MODEL_POLICY_FILE || "/workspace/agent/argus-model-policy.json",
    );
    // The pinned task context names the engine/account the channel leased; it
    // fills the identity when the process environment does not.
    if (!e.ARGUS_ENGINE && snapshot.context?.engine)
      identity = { ...identity, engine: snapshot.context.engine };
    if (snapshot.context?.account_provider)
      identity = { ...identity, account_provider: snapshot.context.account_provider };
    // Policy never switches executor (no silent fallback): it reports whether the
    // caller's choice is the one the leased accounts' priority prefers.
    if (snapshot.context?.preferred_executor)
      identity = {
        ...identity,
        preferred_executor: snapshot.context.preferred_executor,
        matches_preferred: snapshot.context.preferred_executor === executor,
      };
    settings = parseAccountSettings(
      snapshot.context?.executor_settings ?? e.ARGUS_ACCOUNT_SETTINGS,
    );
    if (settings) {
      if (!settingsSupport(executor).length)
        throw new Error(`executor ${executor} cannot use a custom account`);
      identity = {
        ...identity,
        account_label: settings.label,
        account_base_url: settings.base_url,
      };
    }
    // --model, then the task's pin and the account's model (fixed choices: no chain),
    // then the policy chain.
    selection = selectModel(
      snapshot,
      implementationScope(snapshot, executor),
      executor,
      opts.modelAttempt ?? 1,
      opts.fallbackReason,
      opts.model || pinnedModel(snapshot, executor) || settings?.model || undefined,
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

  const localRefusal = localHarnessRefusal(executor, e);
  if (localRefusal)
    finish(78, {
      ...identity,
      blocked: "auth_missing",
      reason: "local_harness_engine_only",
      need: localRefusal,
    });
  if (executor === "claude") {
    const restored = restoreClaudeGatewayAuth(e, claudeStub);
    e = restored.env;
    if (restored.source) identity = { ...identity, auth_source: restored.source };
    if (restored.stub) identity = { ...identity, gateway_stub: restored.stub };
  }
  e = prepareExecutorEnv(executor, e, path.join(cwd, ".argus-delegate-runtime"));
  e = withCliToolsPath(e);

  // Auth first: a missing credential blocks, it never reroutes.
  const auth = authStatus(executor, e, os.homedir(), settings);
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
      settings,
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
      selection.scope,
      executor,
      selection.model_attempt + 1,
      outcome.report.executor_error?.kind || "executor_failed",
    );
  } while (Date.now() < deadline);
  finish(outcome.exit, { ...outcome.report, attempts });
}

/** The body of POST /api/accounts/{id}/usage: identifiers and figures only, never a credential. */
export function usagePayload({ taskId, model, engine, role, usage, key }) {
  const measured = usage.measurement === "measured";
  return {
    task_id: taskId ?? null,
    model: model ? String(model).slice(0, 100) : null,
    engine: engine ? String(engine).slice(0, 50) : null,
    role,
    measurement: usage.measurement,
    input_tokens: measured ? usage.input_tokens : null,
    output_tokens: measured ? usage.output_tokens : null,
    cached_input_tokens: measured ? usage.cached_input_tokens : null,
    cost_usd: measured ? usage.cost_usd : null,
    reason: measured ? null : (usage.reason ?? "unknown"),
    run_key: key,
  };
}

/**
 * Best-effort report to Argus. Never throws, never changes the exit code; the reason is
 * a short code (HTTP status or error name), never a message that could echo a header.
 */
export async function reportUsage({ e, accountId, payload, fetchImpl = fetch }) {
  const base = (e.ARGUS_URL ?? "").trim();
  const token = (e.ARGUS_AGENT_TOKEN ?? "").trim();
  if (!accountId) return { status: "skipped", reason: "no_account_id" };
  if (!base || !token) return { status: "skipped", reason: "no_api_env" };
  try {
    const res = await fetchImpl(
      new URL(`/api/accounts/${encodeURIComponent(accountId)}/usage`, base),
      {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${token}` },
        body: JSON.stringify(payload),
        signal: AbortSignal.timeout(USAGE_POST_TIMEOUT_MS),
      },
    );
    if (res.ok) return { status: "sent", reason: res.status === 204 ? "recorded" : "ok" };
    process.stderr.write(`argus-delegate: usage report failed (http_${res.status})\n`);
    return { status: "failed", reason: `http_${res.status}` };
  } catch (err) {
    const reason = String(err?.name || "error").replace(/[^A-Za-z0-9_]/g, "_");
    process.stderr.write(`argus-delegate: usage report failed (${reason})\n`);
    return { status: "failed", reason };
  }
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
  settings = null,
) {
  const modelChoice = effectiveModel({
    override: opts.model,
    taskModel: pinnedModel(snapshot, executor),
    settings,
    policyModel: selection.configured_model,
  });
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
    { cwd, model: selection.configured_model, readOnly: opts.readOnly, lastMessageFile, settings },
    e,
  );
  e = applySettingsEnv(executor, e, settings, selection.configured_model);
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

  // The live log: readable lines for the host's relay file. No token, no network;
  // the host (argus.ts) posts them. A failure here never touches the run.
  let live = null;
  try {
    const taskId = snapshot.context?.task_id;
    const attempt = Number(snapshot.context?.attempt);
    if (taskId && Number.isSafeInteger(attempt) && attempt >= 1) {
      const relayFile =
        e.ARGUS_TASK_LOG_FILE ||
        path.join(
          path.dirname(e.ARGUS_MODEL_POLICY_FILE || "/workspace/agent/argus-model-policy.json"),
          RELAY_FILE,
        );
      const seconds = (v) => (Number(v) > 0 ? Number(v) * 1000 : undefined);
      live = startLiveLog({
        stdoutFile,
        executor,
        send: createFileRelay({ file: relayFile, taskId, attempt }),
        secrets: [e[CUSTOM_KEY_ENV]?.trim(), ...envSecrets(e)],
        flushMs: seconds(e.ARGUS_LIVELOG_FLUSH_S),
        keepaliveMs: seconds(e.ARGUS_LIVELOG_KEEPALIVE_S),
        intervalMs: seconds(e.ARGUS_LIVELOG_TICK_S),
      });
    }
  } catch {
    live = null;
  }

  // Milestones come from real state, polled while the executor runs and once after it.
  const sink = createSink({ e, out });
  const pending = [];
  const tracker = createTracker({
    repository: (e.ARGUS_REPOSITORY_URL ?? "").trim() || undefined,
    probes: {
      head: () => gitHead(cwd),
      output: () => `${readTail(stdoutFile)}\n${readTail(stderrFile)}`,
      ci: await giteaCiProbe(e),
    },
    emit: (m) => pending.push(sink(m)),
  });
  await tracker.start();
  const pollSeconds = Number(e.ARGUS_MILESTONE_POLL_S) > 0 ? Number(e.ARGUS_MILESTONE_POLL_S) : 15;
  let polling = Promise.resolve();
  const poller = setInterval(() => {
    polling = polling.then(() => tracker.poll());
  }, pollSeconds * 1000);

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
  live?.stop();
  clearInterval(poller);
  await polling;
  await tracker.poll();
  if (waitingInput(fs.readFileSync(stdoutFile, "utf8"))) tracker.once("waiting_input", {});
  await Promise.all(pending);

  const artifacts = [stdoutFile, stderrFile];
  if (fs.existsSync(lastMessageFile)) artifacts.push(lastMessageFile);
  const stdoutText = fs.readFileSync(stdoutFile, "utf8");
  const tailSource = fs.existsSync(lastMessageFile) ? lastMessageFile : stdoutFile;
  const secret = (e[CUSTOM_KEY_ENV] ?? "").trim();
  const redact = (text) => (secret ? text.split(secret).join("[redacted]") : text);
  const tail = redact(fs.readFileSync(tailSource, "utf8")).slice(-TAIL_CHARS);
  const jsonFail = !timedOut && code === 0 ? executorJsonFailure(executor, stdoutText) : null;
  const exit = timedOut ? 124 : interrupted || jsonFail || code !== 0 ? 1 : 0;
  const startedAt = new Date(started).toISOString();
  const taskId = snapshot.context?.task_id ?? null;
  const usage = parseUsage(executor, stdoutText);
  const key = runKey({
    taskId,
    agentId: e.ARGUS_AGENT_ID || os.hostname(),
    engine: identity.engine,
    executor,
    startedAt,
  });
  // The delegate always runs as the executor role: it reports against the executor account.
  const accountId = snapshot.context?.executor_account_id ?? null;
  const payload = usagePayload({
    taskId,
    model: usage.model ?? selection.configured_model,
    engine: executor,
    role: "executor",
    usage,
    key,
  });
  const spoolFile = path.join(out, `usage-${runKeyHash(key)}.json`);
  fs.writeFileSync(spoolFile, JSON.stringify({ account_id: accountId, payload }, null, 2) + "\n");
  let usageReport = await reportUsage({ e, accountId, payload });
  // Inside the worker container there is no Argus token (and there must not be): the NanoClaw
  // host reads this file and posts with its own, after checking the task, attempt and account.
  if (accountId && taskId && usageReport.status !== "sent") {
    const relayFile =
      e.ARGUS_USAGE_RELAY_FILE ||
      path.join(
        path.dirname(e.ARGUS_MODEL_POLICY_FILE || "/workspace/agent/argus-model-policy.json"),
        USAGE_RELAY_FILE,
      );
    const queued = appendUsageRecord(relayFile, {
      source: "delegate",
      role: "executor",
      task_id: taskId,
      attempt: snapshot.context?.attempt ?? null,
      account_id: accountId,
      payload,
    });
    if (queued) usageReport = { status: "queued", reason: "host_relay", direct: usageReport };
  }
  const report = {
    ...identity,
    ...evidence,
    report_file: reportFile,
    status: "finished",
    version,
    model: selection.configured_model,
    model_source: modelChoice.source,
    policy_map: selection.scope,
    provider_block_applied:
      Boolean(settings?.provider) && settingsSupport(executor).includes("provider"),
    cwd,
    read_only: opts.readOnly,
    read_only_enforcement: cmd.readOnlyEnforcement ?? "unknown",
    exit_code: code,
    signal,
    timed_out: timedOut,
    interrupted,
    duration_ms: Date.now() - started,
    artifacts,
    usage,
    run_key: key,
    usage_spool: spoolFile,
    usage_report: usageReport,
    tail,
    stderr_tail: redact(fs.readFileSync(stderrFile, "utf8")).slice(-TAIL_CHARS),
    ...(jsonFail ? { executor_error: { ...jsonFail, detail: redact(jsonFail.detail) } } : {}),
  };
  fs.writeFileSync(reportFile, JSON.stringify(report, null, 2) + "\n");
  return { exit, report };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2));
}
