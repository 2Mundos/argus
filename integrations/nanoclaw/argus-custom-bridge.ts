/**
 * Host-side Anthropic→OpenAI bridge for custom engine leases (custom-harness spec).
 * API keys live only in this process until the lease is released.
 */

import type { CustomLeaseSettings } from "./argus-custom-harness.js";
import { CUSTOM_HARNESS_AUTH_TOKEN } from "./argus-custom-harness.js";
import { syncLocalStrataForward } from "./argus-local-forward.js";
import { startAnthropicOpenAIBridge } from "./engines/anthropic-openai-bridge.mjs";

type BridgeHandle = {
  close: () => Promise<void>;
  containerBaseUrl: string;
};

const active = new Map<string, BridgeHandle>();

function containerFacingUrl(port: number): string {
  return `http://host.docker.internal:${port}`;
}

/** Start or replace the bridge for `groupKey`; pass null settings to stop. */
export async function syncCustomHarnessBridge(
  groupKey: string,
  settings: CustomLeaseSettings | null | undefined,
  apiKey: string | null,
  fetchImpl: typeof fetch = fetch,
): Promise<string | null> {
  const prev = active.get(groupKey);
  if (prev) {
    await prev.close().catch(() => undefined);
    active.delete(groupKey);
  }
  if (!settings?.base_url || !apiKey?.trim()) {
    await syncLocalStrataForward(null);
    return null;
  }
  const bridge = await startAnthropicOpenAIBridge({
    settings: {
      base_url: settings.base_url,
      label: settings.label,
      model: settings.model,
      provider: settings.provider,
    },
    apiKey: apiKey.trim(),
    fetchImpl,
  });
  const containerBaseUrl = containerFacingUrl(bridge.port);
  await syncLocalStrataForward({
    version: 1,
    anthropic_base_url: containerBaseUrl,
    auth_token: CUSTOM_HARNESS_AUTH_TOKEN,
  });
  const handle: BridgeHandle = {
    containerBaseUrl,
    close: async () => {
      await syncLocalStrataForward(null);
      await bridge.close();
    },
  };
  active.set(groupKey, handle);
  return containerBaseUrl;
}

export async function stopAllCustomBridges(): Promise<void> {
  for (const [key, handle] of active) {
    await handle.close().catch(() => undefined);
    active.delete(key);
  }
}
