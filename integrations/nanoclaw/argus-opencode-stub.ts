/** OpenRouter executor authorization, independent of the custom harness bridge. */
import type { ContainerConfig } from "@onecli-sh/sdk";
import {
  OPENCODE_GATEWAY_PATHS,
  OPENCODE_GATEWAY_SENTINEL,
  OPENCODE_GATEWAY_SETTINGS,
} from "./engines/opencode-gateway.mjs";

/** Inspect metadata only; an unassigned, broad or partial scope never authorizes a CLI. */
export async function opencodeExecutorAssigned(
  onecliUrl: string,
  apiKey: string | undefined,
  identifier: string,
  fetchImpl: typeof fetch = fetch,
): Promise<boolean> {
  const get = async (path: string) => {
    const res = await fetchImpl(`${onecliUrl.replace(/\/$/, "")}/api${path}`, {
      headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : {},
    });
    if (!res.ok) throw new Error("gateway metadata unavailable");
    return res.json();
  };
  try {
    const agents = (await get("/agents")) as Array<{
      id: string;
      identifier: string;
      secretMode?: string;
    }>;
    const agent = agents.find((a) => a.identifier === identifier);
    if (!agent || agent.secretMode !== "selective") return false;
    const assigned = new Set((await get(`/agents/${agent.id}/secrets`)) as string[]);
    const secrets = (await get("/secrets")) as Array<{
      id: string;
      name: string;
      type: string;
      hostPattern: string;
      pathPattern: string;
    }>;
    return OPENCODE_GATEWAY_PATHS.every((pathPattern, i) =>
      secrets.some(
        (s) =>
          assigned.has(s.id) &&
          s.name === `argus-${identifier}-custom-executor-${i}` &&
          s.type === "generic" &&
          s.hostPattern === "openrouter.ai" &&
          s.pathPattern === pathPattern,
      ),
    );
  } catch {
    return false;
  }
}

/** Only a harmless sentinel crosses into sessions, including the preflight session. */
export async function withOpencodeDelegateRuntime(
  config: ContainerConfig,
  lookup: () => Promise<boolean>,
): Promise<ContainerConfig> {
  const env = { ...config.env };
  // Preserve the separate local executor lane; never import a provider key from config.
  if (env.ARGUS_LOCAL_HARNESS !== "1" || env.ARGUS_ACCOUNT_API_KEY === OPENCODE_GATEWAY_SENTINEL)
    delete env.ARGUS_ACCOUNT_API_KEY;
  delete env.ARGUS_ACCOUNT_SETTINGS;
  if (await lookup()) {
    env.ARGUS_ACCOUNT_API_KEY = OPENCODE_GATEWAY_SENTINEL;
    env.ARGUS_ACCOUNT_SETTINGS = JSON.stringify(OPENCODE_GATEWAY_SETTINGS);
  }
  return { ...config, env };
}
