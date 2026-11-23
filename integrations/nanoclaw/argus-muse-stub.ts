/**
 * Muse delegated CLI and harness runtime for OneCLI generic secrets.
 *
 * When Argus has assigned the Muse generic secret (argus-accounts.ts,
 * `MUSE_ENDPOINTS`) to this agent group, the trusted host gives the worker a
 * non-secret sentinel `META_API_KEY` — the variable the CLI documents as
 * taking priority over the account login, so no other login in the container
 * can supply a different credential. The gateway swaps in the real leased
 * value on the wire for the Meta API host. An absent lease (secret
 * unassigned) gives no sentinel, so `argus-delegate muse` reports
 * `auth_missing` (muse accounts spec).
 *
 * The secret value itself never enters the worker container.
 */

import type { ContainerConfig } from "@onecli-sh/sdk";

/**
 * Not a credential: a constant the CLI accepts as a key shape so it attempts
 * the request; the gateway replaces it with the leased value on the wire.
 * Mirrored in engines/argus-delegate.mjs.
 */
export const MUSE_SENTINEL_API_KEY = "ARGUS-MUSE-SENTINEL-not-a-credential";

/** Host and path pattern of every endpoint the vault injects on (mirrors MUSE_ENDPOINTS). */
export const MUSE_INJECTED = [["api.meta.ai", "/*"]] as const;

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
 * Whether the Muse generic secret is assigned to this agent group (reads
 * names, types, hosts and paths only, never secret values).
 */
export async function museExecutorAssigned(
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
  const assigned = new Set(secretIds);
  const held = secrets.filter((s) => assigned.has(s.id) && s.type === "generic");
  return MUSE_INJECTED.every(([host, path]) =>
    held.some((s) => s.hostPattern === host && s.pathPattern === path),
  );
}

/**
 * Give the worker the sentinel when `assigned`; without it, no `META_API_KEY`
 * from elsewhere survives.
 */
export function augmentMuseContainerConfig(
  config: ContainerConfig,
  assigned: boolean,
): ContainerConfig {
  const env = { ...config.env };
  delete env.META_API_KEY;
  if (assigned) {
    env.META_API_KEY = MUSE_SENTINEL_API_KEY;
  }
  return { ...config, env };
}

/** After OneCLI returns container config, apply the Muse runtime. */
export async function withMuseDelegateRuntime(
  config: ContainerConfig,
  lookup: () => Promise<boolean>,
): Promise<ContainerConfig> {
  return augmentMuseContainerConfig(config, await lookup());
}
