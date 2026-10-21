/**
 * Host-side Anthropic→OpenAI bridge for custom engine leases (custom-harness spec).
 * API keys live only in this process until the lease is released.
 *
 * Where agent containers reach it ("Bridge reachable from agent containers"):
 * - NanoClaw in a fleet container (`ARGUS_CUSTOM_BRIDGE_PORT` set): the bridge listens
 *   on 0.0.0.0:that port inside the NanoClaw container, which the fleet publishes on
 *   the Docker host's bridge gateway (host.docker.internal for agent containers) at
 *   an ephemeral host port. The container URL names that published port, read with
 *   `docker port`. The NanoClaw container sits on its own network, so its loopback
 *   (and its IP) are unreachable from agent containers: only the published port is.
 * - NanoClaw on the Docker host: loopback bridge plus the docker0 forwarder the
 *   Strata path uses (argus-local-forward).
 * Either way every request must carry the per-bridge key (BRIDGE_KEY_HEADER), so
 * another container that can reach the port cannot spend the account.
 */

import { execFile } from "node:child_process";
import { randomBytes } from "node:crypto";
import { promisify } from "node:util";
import type { CustomLeaseSettings } from "./argus-custom-harness.js";
import { CUSTOM_HARNESS_AUTH_TOKEN } from "./argus-custom-harness.js";
import { syncLocalStrataForward } from "./argus-local-forward.js";
import { startAnthropicOpenAIBridge } from "./engines/anthropic-openai-bridge.mjs";

export { BRIDGE_KEY_HEADER } from "./engines/anthropic-openai-bridge.mjs";

export interface BridgeEndpoint {
  /** URL agent containers use. */
  containerBaseUrl: string;
  /** Per-bridge key the container must send in BRIDGE_KEY_HEADER. */
  bridgeKey: string;
}

type BridgeHandle = BridgeEndpoint & { close: () => Promise<void> };

/** Resolves the host port Docker published for `containerPort` of `container`. */
export type PublishedPortResolver = (container: string, containerPort: number) => Promise<number>;

/** How the bridge is placed: published from the NanoClaw container, or on the host. */
export type BridgePlacement =
  | { kind: "published"; containerPort: number; container: string }
  | { kind: "host" };

const PORT_RE = /^[0-9]{1,5}$/;

export function bridgePlacement(env: NodeJS.ProcessEnv = process.env): BridgePlacement {
  const raw = (env.ARGUS_CUSTOM_BRIDGE_PORT ?? "").trim();
  const container = (env.ARGUS_CUSTOM_BRIDGE_CONTAINER ?? "").trim();
  if (!raw) return { kind: "host" };
  const port = PORT_RE.test(raw) ? Number(raw) : NaN;
  if (!(port >= 1 && port <= 65535) || !container)
    throw new Error(
      "ARGUS_CUSTOM_BRIDGE_PORT needs a valid port and ARGUS_CUSTOM_BRIDGE_CONTAINER",
    );
  return { kind: "published", containerPort: port, container };
}

/** Parse `docker port` output ("172.17.0.1:32768", maybe also an IPv6 line). */
export function parsePublishedPort(output: string): number | null {
  for (const line of output.split("\n")) {
    const m = /:(\d{1,5})\s*$/.exec(line.trim());
    if (m) {
      const p = Number(m[1]);
      if (p >= 1 && p <= 65535) return p;
    }
  }
  return null;
}

const execFileP = promisify(execFile);

export const dockerPublishedPort: PublishedPortResolver = async (container, containerPort) => {
  const { stdout } = await execFileP("docker", ["port", container, `${containerPort}/tcp`], {
    timeout: 10_000,
  });
  const port = parsePublishedPort(stdout);
  if (port === null) throw new Error(`no published port for ${container} ${containerPort}/tcp`);
  return port;
};

const active = new Map<string, BridgeHandle>();

/**
 * Start or replace the bridge for `groupKey`; pass null settings to stop. Returns
 * where agent containers reach it, or null when there is nothing to bridge.
 */
export async function syncCustomHarnessBridge(
  groupKey: string,
  settings: CustomLeaseSettings | null | undefined,
  apiKey: string | null,
  fetchImpl: typeof fetch = fetch,
  opts: { env?: NodeJS.ProcessEnv; resolvePublished?: PublishedPortResolver } = {},
): Promise<BridgeEndpoint | null> {
  const prev = active.get(groupKey);
  if (prev) {
    await prev.close().catch(() => undefined);
    active.delete(groupKey);
  }
  if (!settings?.base_url || !apiKey?.trim()) {
    await syncLocalStrataForward(null);
    return null;
  }
  const placement = bridgePlacement(opts.env ?? process.env);
  const bridgeKey = randomBytes(32).toString("hex");
  const bridge = await startAnthropicOpenAIBridge({
    settings: {
      base_url: settings.base_url,
      label: settings.label,
      model: settings.model,
      provider: settings.provider,
    },
    apiKey: apiKey.trim(),
    bridgeKey,
    fetchImpl,
    ...(placement.kind === "published"
      ? { listenHost: "0.0.0.0", listenPort: placement.containerPort }
      : {}),
  }).catch((err: NodeJS.ErrnoException) => {
    // One published port per NanoClaw container: a second custom group cannot share it.
    if (placement.kind === "published" && err?.code === "EADDRINUSE")
      throw new Error(
        `custom bridge port ${placement.containerPort} is in use: one custom harness per NanoClaw container`,
      );
    throw err;
  });
  let containerBaseUrl: string;
  try {
    if (placement.kind === "published") {
      const resolve = opts.resolvePublished ?? dockerPublishedPort;
      const hostPort = await resolve(placement.container, placement.containerPort);
      containerBaseUrl = `http://host.docker.internal:${hostPort}`;
    } else {
      containerBaseUrl = `http://host.docker.internal:${bridge.port}`;
      await syncLocalStrataForward({
        version: 1,
        anthropic_base_url: containerBaseUrl,
        auth_token: CUSTOM_HARNESS_AUTH_TOKEN,
      });
    }
  } catch (err) {
    await bridge.close().catch(() => undefined);
    throw err;
  }
  const handle: BridgeHandle = {
    containerBaseUrl,
    bridgeKey,
    close: async () => {
      if (placement.kind === "host") await syncLocalStrataForward(null);
      await bridge.close();
    },
  };
  active.set(groupKey, handle);
  return { containerBaseUrl, bridgeKey };
}

/** Whether this process runs a bridge for `groupKey` now (its harness file is live). */
export function hasCustomBridge(groupKey: string): boolean {
  return active.has(groupKey);
}

export async function stopAllCustomBridges(): Promise<void> {
  for (const [key, handle] of active) {
    await handle.close().catch(() => undefined);
    active.delete(key);
  }
}
