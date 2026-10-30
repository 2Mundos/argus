/**
 * GitHub Copilot delegated CLI runtime for OneCLI generic secrets.
 *
 * When Argus has assigned every Copilot generic secret (argus-accounts.ts,
 * `COPILOT_ENDPOINTS`) to this agent group, the trusted host gives the worker a
 * non-secret sentinel `COPILOT_GITHUB_TOKEN`; the gateway swaps in the real token
 * on the wire for exactly those hosts and paths. An absent or stale lease (secrets
 * unassigned, or only some of them) gives no sentinel, so `argus-delegate copilot`
 * reports `auth_missing` (copilot accounts spec).
 */

import type { ContainerConfig } from "@onecli-sh/sdk";

/**
 * Not a credential: a constant shaped like a fine-grained PAT so the pinned CLI
 * (1.0.105) accepts its prefix client-side. Mirrored in engines/argus-delegate.mjs.
 */
export const COPILOT_SENTINEL_TOKEN =
  "github_pat_ARGUS_ONECLI_SENTINEL_not_a_credential_0000000000000000000000000000000000";

/** Container-local root, never a mounted path; argus-delegate uses a per-run home anyway. */
export const COPILOT_HOME_CONTAINER_DIR = "/tmp/argus-copilot-home";

/** Host and exact path of every endpoint the vault injects on (mirrors COPILOT_ENDPOINTS). */
export const COPILOT_INJECTED = [
  ["api.github.com", "/copilot_internal/user"],
  ["api.individual.githubcopilot.com", "/models"],
  ["api.individual.githubcopilot.com", "/responses"],
  ["api.individual.githubcopilot.com", "/chat/completions"],
  ["api.individual.githubcopilot.com", "/v1/messages"],
] as const;

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
 * Whether every Copilot generic secret is assigned to this agent group (reads
 * names, types, hosts and paths only, never secret values).
 */
export async function copilotExecutorAssigned(
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
  return COPILOT_INJECTED.every(([host, path]) =>
    held.some((s) => s.hostPattern === host && s.pathPattern === path),
  );
}

/**
 * Give the worker the sentinel and a container-local home when `assigned`; without
 * it, no `COPILOT_GITHUB_TOKEN` from elsewhere survives (`GH_TOKEN`/`GITHUB_TOKEN`
 * may serve other tools and are scrubbed by argus-delegate for the Copilot child).
 */
export function augmentCopilotContainerConfig(
  config: ContainerConfig,
  assigned: boolean,
): ContainerConfig {
  const env = { ...config.env };
  delete env.COPILOT_GITHUB_TOKEN;
  delete env.COPILOT_HOME;
  if (assigned) {
    env.COPILOT_GITHUB_TOKEN = COPILOT_SENTINEL_TOKEN;
    env.COPILOT_HOME = COPILOT_HOME_CONTAINER_DIR;
  }
  return { ...config, env };
}

/** After OneCLI returns container config, apply the Copilot executor runtime. */
export async function withCopilotDelegateRuntime(
  config: ContainerConfig,
  lookup: () => Promise<boolean>,
): Promise<ContainerConfig> {
  return augmentCopilotContainerConfig(config, await lookup());
}
