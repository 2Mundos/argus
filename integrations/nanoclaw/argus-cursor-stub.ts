/**
 * Cursor delegated CLI runtime for OneCLI generic secrets.
 *
 * Generic Cursor secrets do not appear in `container-config` env (no `CURSOR_*`);
 * the gateway only adds proxy + CA. When Argus has assigned `api2.cursor.sh`
 * generic secrets to this agent group, the trusted host mounts sentinel
 * `auth.json` (access + refresh placeholders) and `cli-config.json` (HTTP/1.1).
 * The real access token never enters the worker; injection is path-scoped on the
 * gateway (cursor accounts spec).
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ContainerConfig } from "@onecli-sh/sdk";

export const CURSOR_HOST = "api2.cursor.sh";
export const CURSOR_PATH_AGENT = "/agent.v1.*";
export const CURSOR_PATH_AISERVER = "/aiserver.v1.*";

/** NanoClaw agent image user (container/Dockerfile). */
export const CURSOR_CONFIG_CONTAINER_DIR = "/home/node/.config/cursor";
export const CURSOR_AUTH_CONTAINER_PATH = `${CURSOR_CONFIG_CONTAINER_DIR}/auth.json`;
export const CURSOR_CLI_CONFIG_CONTAINER_PATH = `${CURSOR_CONFIG_CONTAINER_DIR}/cli-config.json`;

/** Fake JWT-shaped sentinel the CLI treats as a session; gateway replaces on the wire. */
export const CURSOR_SENTINEL_ACCESS_TOKEN =
  "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJhdXRoMHxzZW50aW5lbCIsImlzcyI6Imh0dHBzOi8vYXV0aGVudGljYXRpb24uY3Vyc29yLnNoIiwiYXVkIjoiaHR0cHM6Ly9jdXJzb3IuY29tIiwiZXhwIjoyMTA3NjQ0NTgyLCJpYXQiOjEsInR5cGUiOiJzZXNzaW9uIn0.sentinel";

const CURSOR_SENTINEL_REFRESH_TOKEN = "onecli-managed-refresh-not-a-credential";

const CLI_CONFIG_STUB = JSON.stringify(
  { version: 1, network: { useHttp1ForAgent: true } },
  null,
  2,
);

export function cursorAuthStubContent(): string {
  return JSON.stringify(
    {
      accessToken: CURSOR_SENTINEL_ACCESS_TOKEN,
      refreshToken: CURSOR_SENTINEL_REFRESH_TOKEN,
    },
    null,
    2,
  );
}

type SecretMeta = {
  id: string;
  name?: string;
  type?: string;
  hostPattern?: string;
  pathPattern?: string;
};

/** True when a secret is an Argus Cursor executor generic injection rule (metadata only). */
export function isCursorGenericSecret(secret: SecretMeta): boolean {
  return (
    secret.type === "generic" &&
    secret.hostPattern === CURSOR_HOST &&
    (secret.pathPattern === CURSOR_PATH_AGENT || secret.pathPattern === CURSOR_PATH_AISERVER)
  );
}

export function argusCursorAgentSecretName(agentGroupId: string): string {
  return `argus-${agentGroupId}-cursor-agent`;
}

async function onecliJson<T>(
  url: string,
  apiKey: string | undefined,
  fetchImpl: typeof fetch,
): Promise<T | null> {
  try {
    const res = await fetchImpl(url, {
      headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : {},
    });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

/**
 * Whether this agent group currently has Cursor generic secrets assigned in
 * selective mode (reads names/types/paths only, never secret values).
 */
export async function cursorExecutorAssigned(
  onecliUrl: string,
  apiKey: string | undefined,
  agentGroupId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<boolean> {
  const base = onecliUrl.replace(/\/$/, "");
  const agents = await onecliJson<Array<{ id: string; identifier?: string }>>(
    `${base}/api/agents`,
    apiKey,
    fetchImpl,
  );
  const agent = agents?.find((a) => a.identifier === agentGroupId);
  if (!agent?.id) return false;
  const secretIds = await onecliJson<string[]>(
    `${base}/api/agents/${agent.id}/secrets`,
    apiKey,
    fetchImpl,
  );
  if (!secretIds?.length) return false;
  const secrets = await onecliJson<SecretMeta[]>(`${base}/api/secrets`, apiKey, fetchImpl);
  if (!secrets?.length) return false;
  const idSet = new Set(secretIds);
  return secrets.some((s) => idSet.has(s.id) && isCursorGenericSecret(s));
}

const isCursorAuthStub = (stub: { containerPath: string }) =>
  stub.containerPath === CURSOR_AUTH_CONTAINER_PATH ||
  stub.containerPath.endsWith("/cursor/auth.json");

const isCursorCliConfigStub = (stub: { containerPath: string }) =>
  stub.containerPath === CURSOR_CLI_CONFIG_CONTAINER_PATH ||
  stub.containerPath.endsWith("/cursor/cli-config.json");

/** Merge HTTP/1.1 for the agent into an existing cli-config stub, if any. */
export function ensureHttp1CliConfig(content: string): string {
  try {
    const doc = JSON.parse(content);
    if (!doc || typeof doc !== "object" || Array.isArray(doc)) return CLI_CONFIG_STUB;
    const network =
      doc.network && typeof doc.network === "object" && !Array.isArray(doc.network)
        ? doc.network
        : {};
    return JSON.stringify(
      { ...doc, version: doc.version ?? 1, network: { ...network, useHttp1ForAgent: true } },
      null,
      2,
    );
  } catch {
    return CLI_CONFIG_STUB;
  }
}

/**
 * Add Cursor sentinel stubs when `assigned` is true. Never sets `CURSOR_API_KEY`
 * (that would make the CLI call `/auth/exchange_user_api_key` inside the worker).
 */
export function augmentCursorContainerConfig(
  config: ContainerConfig,
  assigned: boolean,
): ContainerConfig {
  if (!assigned) return config;
  const stubs = [...(config.credentialStubs ?? [])];
  let auth = stubs.find(isCursorAuthStub);
  if (!auth) {
    auth = { containerPath: CURSOR_AUTH_CONTAINER_PATH, content: cursorAuthStubContent() };
    stubs.push(auth);
  } else {
    auth.content = cursorAuthStubContent();
  }
  let cli = stubs.find(isCursorCliConfigStub);
  if (!cli) {
    cli = { containerPath: CURSOR_CLI_CONFIG_CONTAINER_PATH, content: CLI_CONFIG_STUB };
    stubs.push(cli);
  } else {
    cli.content = ensureHttp1CliConfig(cli.content);
  }
  const env = { ...config.env };
  delete env.CURSOR_API_KEY;
  return {
    ...config,
    env: { ...env, CURSOR_CONFIG_DIR: CURSOR_CONFIG_CONTAINER_DIR },
    credentialStubs: stubs,
  };
}

/**
 * After OneCLI returns container config, optionally add Cursor sentinel stubs when
 * this agent group has assigned Cursor generic secrets.
 */
export async function withCursorDelegateRuntime(
  config: ContainerConfig,
  lookup: () => Promise<boolean>,
): Promise<ContainerConfig> {
  if (!(await lookup())) return config;
  return augmentCursorContainerConfig(config, true);
}

/** Replayable fixture fetch for tests (no live OneCLI). */
export function fixtureOnecliFetch(fixturesDir: string): typeof fetch {
  const read = (name: string) => readFileSync(join(fixturesDir, name), "utf8");
  return (async (url: string) => {
    const u = new URL(url);
    const path = u.pathname.replace(/\/$/, "");
    if (path === "/api/agents") {
      return new Response(read("agents.json"), { status: 200 });
    }
    if (path === "/api/secrets") {
      return new Response(read("secrets-metadata.json"), { status: 200 });
    }
    const agentSecrets = /^\/api\/agents\/([^/]+)\/secrets$/.exec(path);
    if (agentSecrets) {
      const agents = JSON.parse(read("agents.json")) as Array<{ id: string; identifier?: string }>;
      const scratch = agents.find((a) => a.identifier === "scratch-agent");
      if (scratch && agentSecrets[1] === scratch.id) {
        return new Response(read("scratch-agent-secret-ids.json"), { status: 200 });
      }
      return new Response("[]", { status: 200 });
    }
    return new Response("not found", { status: 404 });
  }) as typeof fetch;
}

export const gatewayFixturesDir = join(
  dirname(fileURLToPath(import.meta.url)),
  "fixtures/cursor-gateway",
);
