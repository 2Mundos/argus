/**
 * Google Antigravity (`agy`) delegated CLI runtime for OneCLI generic secrets.
 *
 * Two mutually exclusive modes mirror the leased `google` account:
 * - API key mode: OneCLI assigns `generativelanguage.googleapis.com` `/v1beta/models*`
 *   with `x-goog-api-key` injection; the trusted host gives the worker the non-secret
 *   sentinel `GEMINI_API_KEY` and strips any OAuth stub.
 * - OAuth subscription mode: OneCLI assigns `www.googleapis.com`,
 *   `daily-cloudcode-pa.googleapis.com` and `cloudcode-pa.googleapis.com` Bearer
 *   secrets; the trusted host mounts a read-only sentinel `antigravity-oauth-token`
 *   with a far-future expiry (so the CLI never attempts a refresh in the container)
 *   and strips `GEMINI_API_KEY`.
 *
 * Neither secret value ever enters the worker container (google accounts spec).
 */

import type { ContainerConfig } from "@onecli-sh/sdk";

/**
 * Not a credential: a constant shaped like a Gemini API key; the gateway swaps in
 * the real key on the wire (`x-goog-api-key`). Mirrored in engines/argus-delegate.mjs.
 */
export const AGY_SENTINEL_API_KEY = "AIzaSyARGUS_ONECLI_SENTINEL_not_a_credential_00000";

/**
 * Not a credential: fake Google OAuth access token placed inside the read-only stub;
 * the gateway swaps in the real Bearer token on the wire. Mirrored in engines/argus-delegate.mjs.
 */
export const AGY_SENTINEL_OAUTH_TOKEN = "ya29.ARGUS_ONECLI_SENTINEL_not_a_credential";

const AGY_SENTINEL_REFRESH_TOKEN = "1//onecli-managed-refresh-not-a-credential";

/** NanoClaw agent image user (container/Dockerfile). */
export const AGY_OAUTH_CONTAINER_PATH =
  "/home/node/.gemini/antigravity-cli/antigravity-oauth-token";

export const AGY_GEMINI_HOST = "generativelanguage.googleapis.com";
export const AGY_GEMINI_PATH = "/v1beta/models*";

/** Host and path pattern for every Google OAuth endpoint the vault injects on. */
export const AGY_OAUTH_INJECTED = [
  ["www.googleapis.com", "/oauth2/v2/userinfo"],
  ["daily-cloudcode-pa.googleapis.com", "/v1internal*"],
  ["cloudcode-pa.googleapis.com", "/v1internal*"],
] as const;

export type AgyAuthMode = "api_key" | "oauth" | null;

export function agyOauthStubContent(): string {
  return JSON.stringify(
    {
      token: {
        access_token: AGY_SENTINEL_OAUTH_TOKEN,
        token_type: "Bearer",
        refresh_token: AGY_SENTINEL_REFRESH_TOKEN,
        expiry: "2099-01-01T00:00:00Z",
        expires_in: 3600,
      },
      auth_method: "consumer",
    },
    null,
    2,
  );
}

type SecretMeta = {
  id: string;
  type?: string;
  hostPattern?: string;
  pathPattern?: string;
};

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
 * Which Google/Antigravity generic secret set is assigned to this agent group
 * (reads names, types, hosts and paths only, never secret values).
 */
export async function agyExecutorAssigned(
  onecliUrl: string,
  apiKey: string | undefined,
  agentGroupId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<AgyAuthMode> {
  const base = onecliUrl.replace(/\/$/, "");
  const agents = await onecliJson<Array<{ id: string; identifier?: string }>>(
    `${base}/api/agents`,
    apiKey,
    fetchImpl,
  );
  const agent = agents?.find((a) => a.identifier === agentGroupId);
  if (!agent?.id) return null;
  const secretIds = await onecliJson<string[]>(
    `${base}/api/agents/${agent.id}/secrets`,
    apiKey,
    fetchImpl,
  );
  if (!secretIds?.length) return null;
  const secrets = await onecliJson<SecretMeta[]>(`${base}/api/secrets`, apiKey, fetchImpl);
  if (!secrets?.length) return null;
  const assigned = new Set(secretIds);
  const held = secrets.filter((s) => assigned.has(s.id) && s.type === "generic");
  if (held.some((s) => s.hostPattern === AGY_GEMINI_HOST && s.pathPattern === AGY_GEMINI_PATH)) {
    return "api_key";
  }
  if (
    AGY_OAUTH_INJECTED.every(([host, path]) =>
      held.some((s) => s.hostPattern === host && s.pathPattern === path),
    )
  ) {
    return "oauth";
  }
  return null;
}

const isAgyOauthStub = (stub: { containerPath: string }) =>
  stub.containerPath === AGY_OAUTH_CONTAINER_PATH ||
  stub.containerPath.endsWith("/antigravity-cli/antigravity-oauth-token");

/**
 * Give the worker only the sentinel matching `mode`; without an assigned lease
 * (or when switching modes) neither `GEMINI_API_KEY` nor an OAuth stub survives.
 */
export function augmentAgyContainerConfig(
  config: ContainerConfig,
  mode: AgyAuthMode,
): ContainerConfig {
  const env = { ...config.env };
  delete env.GEMINI_API_KEY;
  delete env.ARGUS_AGY_OAUTH_STUB;
  const stubs = (config.credentialStubs ?? []).filter((s) => !isAgyOauthStub(s));
  if (mode === "api_key") {
    env.GEMINI_API_KEY = AGY_SENTINEL_API_KEY;
  } else if (mode === "oauth") {
    env.ARGUS_AGY_OAUTH_STUB = AGY_OAUTH_CONTAINER_PATH;
    stubs.push({
      containerPath: AGY_OAUTH_CONTAINER_PATH,
      content: agyOauthStubContent(),
    });
  }
  return {
    ...config,
    env,
    ...(config.credentialStubs !== undefined || stubs.length > 0 ? { credentialStubs: stubs } : {}),
  };
}

/** After OneCLI returns container config, apply the Antigravity executor runtime. */
export async function withAgyDelegateRuntime(
  config: ContainerConfig,
  lookup: () => Promise<AgyAuthMode>,
): Promise<ContainerConfig> {
  return augmentAgyContainerConfig(config, await lookup());
}
