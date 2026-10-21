/**
 * NanoClaw harness on a custom OpenAI-compatible account via a host loopback bridge.
 * See docs/specs/custom-harness.spec.md.
 */
import { readFileSync, unlinkSync } from "node:fs";
import { mkdir, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import type { ContainerConfig } from "@onecli-sh/sdk";
import { CLAUDE_GATEWAY_STUB_PATH } from "./argus-claude-stub.js";
import {
  CUSTOM_HARNESS_AUTH_TOKEN,
  normalizeAnthropicBaseUrl,
} from "./engines/anthropic-redirect.mjs";

export { CUSTOM_HARNESS_AUTH_TOKEN } from "./engines/anthropic-redirect.mjs";

export const CUSTOM_HARNESS_FILENAME = "argus-custom-harness.json";

export interface CustomHarnessConfig {
  version: 1;
  /** URL the container uses (host.docker.internal bridge port). */
  anthropic_base_url: string;
  model?: string;
  /** Per-bridge key the container sends in the bridge key header. */
  bridge_key?: string;
}

/** Header name the bridge checks (engines/anthropic-openai-bridge.mjs BRIDGE_KEY_HEADER). */
const BRIDGE_KEY_HEADER_NAME = "x-argus-bridge-key";
const BRIDGE_KEY_RE = /^[0-9a-f]{32,128}$/;

export interface CustomLeaseSettings {
  base_url: string;
  label: string | null;
  model: string | null;
  provider: Record<string, unknown> | null;
}

export function customHarnessFromLease(
  containerBaseUrl: string,
  settings: CustomLeaseSettings | null | undefined,
  bridgeKey?: string | null,
): CustomHarnessConfig | null {
  const base = normalizeAnthropicBaseUrl(containerBaseUrl);
  if (!base || !settings?.base_url) return null;
  const model =
    typeof settings.model === "string" && settings.model.trim() ? settings.model.trim() : undefined;
  const key =
    typeof bridgeKey === "string" && BRIDGE_KEY_RE.test(bridgeKey) ? bridgeKey : undefined;
  return {
    version: 1,
    anthropic_base_url: base,
    ...(model ? { model } : {}),
    ...(key ? { bridge_key: key } : {}),
  };
}

export function customHarnessEnv(harness: CustomHarnessConfig): Record<string, string> {
  const base = normalizeAnthropicBaseUrl(harness.anthropic_base_url);
  if (!base) throw new Error("invalid custom harness base URL");
  const out: Record<string, string> = {
    ANTHROPIC_BASE_URL: base,
    ANTHROPIC_AUTH_TOKEN: CUSTOM_HARNESS_AUTH_TOKEN,
    ARGUS_ALLOWED_ANTHROPIC_BASE_URL: base,
  };
  if (harness.model) out.ANTHROPIC_MODEL = harness.model;
  if (harness.bridge_key)
    out.ANTHROPIC_CUSTOM_HEADERS = `${BRIDGE_KEY_HEADER_NAME}: ${harness.bridge_key}`;
  return out;
}

/** Drop cloud Anthropic material; the bridge is reached directly. */
export function withCustomHarnessRuntime(
  config: ContainerConfig,
  harness: CustomHarnessConfig | null,
): ContainerConfig {
  const stubs = (config.credentialStubs ?? []).filter(
    (stub) => stub.containerPath !== CLAUDE_GATEWAY_STUB_PATH,
  );
  if (!harness) {
    const env = { ...(config.env ?? {}) };
    for (const key of [
      "ANTHROPIC_BASE_URL",
      "ANTHROPIC_AUTH_TOKEN",
      "ARGUS_ALLOWED_ANTHROPIC_BASE_URL",
      "ANTHROPIC_MODEL",
      "ANTHROPIC_CUSTOM_HEADERS",
    ])
      delete env[key];
    return { ...config, env };
  }
  const env = { ...(config.env ?? {}) };
  for (const key of ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_UNIX_SOCKET"])
    delete env[key];
  const base = normalizeAnthropicBaseUrl(harness.anthropic_base_url)!;
  const host = new URL(base).hostname;
  const mergeNoProxy = (existing: string | undefined, hosts: string[]) => {
    const set = new Set(
      (existing ?? "")
        .split(",")
        .map((h) => h.trim())
        .filter(Boolean),
    );
    for (const h of hosts) set.add(h);
    return [...set].join(",");
  };
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
      ...customHarnessEnv(harness),
      NO_PROXY: noProxy,
      no_proxy: noProxy,
    },
  };
}

export function customHarnessPath(dataDir: string, groupFolder: string): string {
  return path.join(dataDir, "groups", groupFolder, CUSTOM_HARNESS_FILENAME);
}

export function readCustomHarnessFile(
  dataDir: string,
  groupFolder: string,
): CustomHarnessConfig | null {
  try {
    const raw = JSON.parse(readFileSync(customHarnessPath(dataDir, groupFolder), "utf8"));
    if (raw?.version !== 1 || typeof raw.anthropic_base_url !== "string") return null;
    const base = normalizeAnthropicBaseUrl(raw.anthropic_base_url);
    if (!base) return null;
    return {
      version: 1,
      anthropic_base_url: base,
      ...(typeof raw.model === "string" && raw.model.trim() ? { model: raw.model.trim() } : {}),
      ...(typeof raw.bridge_key === "string" && BRIDGE_KEY_RE.test(raw.bridge_key)
        ? { bridge_key: raw.bridge_key }
        : {}),
    };
  } catch {
    return null;
  }
}

export async function writeCustomHarnessFile(
  dataDir: string,
  groupFolder: string,
  harness: CustomHarnessConfig,
): Promise<void> {
  const dir = path.join(dataDir, "groups", groupFolder);
  await mkdir(dir, { recursive: true });
  const file = customHarnessPath(dataDir, groupFolder);
  const tmp = `${file}.tmp`;
  await writeFile(tmp, `${JSON.stringify(harness, null, 2)}\n`);
  await rename(tmp, file);
}

export function removeCustomHarnessFile(dataDir: string, groupFolder: string): void {
  try {
    unlinkSync(customHarnessPath(dataDir, groupFolder));
  } catch {
    /* absent */
  }
}
