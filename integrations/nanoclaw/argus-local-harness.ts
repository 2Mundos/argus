/**
 * NanoClaw harness on the node's local Anthropic-compatible model (Strata).
 *
 * The worker talks to `host.docker.internal` (or loopback on the host), never
 * through the OneCLI cloud gateway. The trusted host materialises
 * `ANTHROPIC_BASE_URL`, a placeholder token and the matching allowlist entry
 * the delegate checks (`ARGUS_ALLOWED_ANTHROPIC_BASE_URL`).
 */
import { readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { mkdir, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import type { ContainerConfig } from "@onecli-sh/sdk";
import { CLAUDE_GATEWAY_STUB_PATH } from "./argus-claude-stub.js";
import {
  LOCAL_HARNESS_AUTH_TOKEN,
  normalizeAnthropicBaseUrl,
} from "./engines/anthropic-redirect.mjs";

export { LOCAL_HARNESS_AUTH_TOKEN };

export const LOCAL_HARNESS_FILENAME = "argus-local-harness.json";

const ALLOWED_HOSTS = new Set(["127.0.0.1", "localhost", "::1", "host.docker.internal"]);

function mergeNoProxy(existing: string | undefined, hosts: string[]): string {
  const set = new Set(
    (existing ?? "")
      .split(",")
      .map((h) => h.trim())
      .filter(Boolean),
  );
  for (const h of hosts) set.add(h);
  return [...set].join(",");
}

export interface LocalHarnessConfig {
  version: 1;
  anthropic_base_url: string;
  auth_token: string;
  model?: string;
  /** The local model's context window in tokens (account setting, else the probe). */
  context_tokens?: number;
}

/** Assumed when neither the account nor the probe names the window: small, safe. */
export const DEFAULT_LOCAL_CONTEXT_TOKENS = 32768;
const CONTEXT_RANGE = [4096, 4_000_000] as const;
/** Auto-compact at this share of the window: headroom for the reply and for the
 * gap between Claude Code's token estimate and the local tokenizer. */
const COMPACT_FRACTION = 0.7;
/** Tool output caps on the small local context (Read tokens, Bash characters). */
const LOCAL_FILE_READ_MAX_TOKENS = 4000;
const LOCAL_BASH_MAX_OUTPUT_CHARS = 8000;

/** Env names this module sets for a local harness (and clears otherwise). */
export const LOCAL_HARNESS_ENV_KEYS = [
  "ANTHROPIC_BASE_URL",
  "ANTHROPIC_AUTH_TOKEN",
  "ARGUS_ALLOWED_ANTHROPIC_BASE_URL",
  "ANTHROPIC_MODEL",
  "ARGUS_LOCAL_HARNESS",
  "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
  "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
  "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
  "CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS",
  "BASH_MAX_OUTPUT_LENGTH",
] as const;

const validContext = (v: unknown): v is number =>
  typeof v === "number" && Number.isInteger(v) && v >= CONTEXT_RANGE[0] && v <= CONTEXT_RANGE[1];

/**
 * Claude Code 2.1.296 knobs (verified in the bundled binary): for a model it does not
 * know, `CLAUDE_CODE_MAX_CONTEXT_TOKENS` is the window (otherwise it assumes 200k
 * and never compacts before a 64k server refuses the prompt);
 * `CLAUDE_CODE_AUTO_COMPACT_WINDOW` caps the window auto-compact works from, which
 * then reserves `min(max output, 20k)` for the reply.
 */
export function localContextEnv(contextTokens: number | undefined): Record<string, string> {
  const window = validContext(contextTokens) ? contextTokens : DEFAULT_LOCAL_CONTEXT_TOKENS;
  const output = Math.min(8192, Math.floor(window / 8));
  return {
    CLAUDE_CODE_MAX_CONTEXT_TOKENS: String(window),
    CLAUDE_CODE_AUTO_COMPACT_WINDOW: String(Math.floor(window * COMPACT_FRACTION)),
    CLAUDE_CODE_MAX_OUTPUT_TOKENS: String(output),
    CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS: String(LOCAL_FILE_READ_MAX_TOKENS),
    BASH_MAX_OUTPUT_LENGTH: String(LOCAL_BASH_MAX_OUTPUT_CHARS),
  };
}

export function localCredentialProblem(credential: Record<string, unknown>): string | null {
  const keys = new Set(Object.keys(credential));
  const allowed = new Set(["base_url", "auth_token", "model", "context_tokens"]);
  const extra = [...keys].filter((k) => !allowed.has(k)).sort();
  if (extra.length)
    return `a local account credential holds only base_url, auth_token, model, context_tokens; remove ${extra.join(", ")}`;
  const base = normalizeAnthropicBaseUrl(String(credential.base_url ?? ""));
  if (!base) return "base_url must be an http(s) loopback or host.docker.internal URL";
  try {
    const host = new URL(base).hostname.toLowerCase();
    if (!ALLOWED_HOSTS.has(host)) return "base_url host must be loopback or host.docker.internal";
  } catch {
    return "base_url must be an http(s) URL";
  }
  const token =
    typeof credential.auth_token === "string" && credential.auth_token.trim()
      ? credential.auth_token.trim()
      : LOCAL_HARNESS_AUTH_TOKEN;
  if (!/^[A-Za-z0-9._-]{1,64}$/.test(token) || token.startsWith("sk-"))
    return "auth_token must be a short placeholder, never a credential";
  const model = credential.model;
  if (
    model !== undefined &&
    (typeof model !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$/.test(model))
  )
    return "model must be a plain model name";
  if (credential.context_tokens !== undefined && !validContext(credential.context_tokens))
    return `context_tokens must be an integer from ${CONTEXT_RANGE[0]} to ${CONTEXT_RANGE[1]}`;
  return null;
}

export function localHarnessFromCredential(
  credential: Record<string, unknown>,
): LocalHarnessConfig | null {
  if (localCredentialProblem(credential)) return null;
  const base = normalizeAnthropicBaseUrl(String(credential.base_url))!;
  const auth_token =
    typeof credential.auth_token === "string" && credential.auth_token.trim()
      ? credential.auth_token.trim()
      : LOCAL_HARNESS_AUTH_TOKEN;
  const model =
    typeof credential.model === "string" && credential.model.trim()
      ? credential.model.trim()
      : undefined;
  const context_tokens = validContext(credential.context_tokens)
    ? credential.context_tokens
    : undefined;
  return {
    version: 1,
    anthropic_base_url: base,
    auth_token,
    ...(model ? { model } : {}),
    ...(context_tokens ? { context_tokens } : {}),
  };
}

export function localHarnessEnv(harness: LocalHarnessConfig): Record<string, string> {
  const base = normalizeAnthropicBaseUrl(harness.anthropic_base_url);
  if (!base) throw new Error("invalid local harness base URL");
  const out: Record<string, string> = {
    ANTHROPIC_BASE_URL: base,
    ANTHROPIC_AUTH_TOKEN: harness.auth_token,
    ARGUS_ALLOWED_ANTHROPIC_BASE_URL: base,
    // The delegate refuses `claude` as an executor here: Strata serves only the harness.
    ARGUS_LOCAL_HARNESS: "1",
    ...localContextEnv(harness.context_tokens),
  };
  if (harness.model) out.ANTHROPIC_MODEL = harness.model;
  return out;
}

/** Drop cloud Anthropic material; the local server is reached directly. */
export function withLocalHarnessRuntime(
  config: ContainerConfig,
  harness: LocalHarnessConfig | null,
): ContainerConfig {
  const stubs = (config.credentialStubs ?? []).filter(
    (stub) => stub.containerPath !== CLAUDE_GATEWAY_STUB_PATH,
  );
  if (!harness) {
    const env = { ...(config.env ?? {}) };
    for (const key of LOCAL_HARNESS_ENV_KEYS) delete env[key];
    // Cloud mode: keep the Claude gateway stub (argus-claude-stub.ts adds it only
    // for a real gateway sentinel); dropping it leaves `argus-delegate claude`
    // with auth_missing. Only local harness mode removes it.
    return { ...config, env };
  }
  const env = { ...(config.env ?? {}) };
  for (const key of ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_UNIX_SOCKET"])
    delete env[key];
  const base = normalizeAnthropicBaseUrl(harness.anthropic_base_url)!;
  const host = new URL(base).hostname;
  const noProxy = mergeNoProxy(env.NO_PROXY ?? env.no_proxy, [
    host,
    "localhost",
    "127.0.0.1",
    "::1",
    "host.docker.internal",
  ]);
  return {
    ...config,
    credentialStubs: stubs,
    env: {
      ...env,
      ...localHarnessEnv(harness),
      NO_PROXY: noProxy,
      no_proxy: noProxy,
    },
  };
}

export function localHarnessPath(dataDir: string, groupFolder: string): string {
  return path.join(dataDir, "groups", groupFolder, LOCAL_HARNESS_FILENAME);
}

export function readLocalHarnessFile(
  dataDir: string,
  groupFolder: string,
): LocalHarnessConfig | null {
  try {
    const raw = JSON.parse(readFileSync(localHarnessPath(dataDir, groupFolder), "utf8"));
    if (raw?.version !== 1 || typeof raw.anthropic_base_url !== "string") return null;
    return localHarnessFromCredential({
      base_url: raw.anthropic_base_url,
      auth_token: raw.auth_token,
      model: raw.model,
      ...(raw.context_tokens !== undefined ? { context_tokens: raw.context_tokens } : {}),
    });
  } catch {
    return null;
  }
}

export async function writeLocalHarnessFile(
  dataDir: string,
  groupFolder: string,
  harness: LocalHarnessConfig,
): Promise<void> {
  const dir = path.join(dataDir, "groups", groupFolder);
  await mkdir(dir, { recursive: true });
  const file = localHarnessPath(dataDir, groupFolder);
  const tmp = `${file}.tmp`;
  await writeFile(tmp, `${JSON.stringify(harness, null, 2)}\n`);
  await rename(tmp, file);
}

export function removeLocalHarnessFile(dataDir: string, groupFolder: string): void {
  try {
    unlinkSync(localHarnessPath(dataDir, groupFolder));
  } catch {
    /* absent */
  }
}

/** Read harness files the Argus channel wrote for this agent group, if any. */
export async function withLocalHarnessRuntimeForGroup(
  config: ContainerConfig,
  dataDir: string,
  agentGroupId: string,
): Promise<ContainerConfig> {
  const { getAgentGroup } = await import("../db/agent-groups.js");
  const group = await getAgentGroup(agentGroupId);
  if (!group) return config;
  const { readCustomHarnessFile, withCustomHarnessRuntime } = await import(
    "./argus-custom-harness.js"
  );
  const custom = readCustomHarnessFile(dataDir, group.folder);
  if (custom) return withCustomHarnessRuntime(config, custom);
  const harness = readLocalHarnessFile(dataDir, group.folder);
  return withLocalHarnessRuntime(config, harness);
}
