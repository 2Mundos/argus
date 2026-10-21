/**
 * TCP forward from the Docker bridge address to loopback so agent containers can
 * reach Strata on 127.0.0.1 without binding the model on the LAN.
 */
import net from "node:net";
import { networkInterfaces } from "node:os";
import type { LocalHarnessConfig } from "./argus-local-harness.js";
import { normalizeAnthropicBaseUrl } from "./engines/anthropic-redirect.mjs";

export interface LocalForwardSpec {
  bindHost: string;
  port: number;
  targetHost: string;
  targetPort: number;
}

/** IPv4 address of docker0 (the bridge gateway containers use as host.docker.internal). */
export function dockerBridgeIpv4(): string | null {
  const addrs = networkInterfaces().docker0;
  if (!addrs) return null;
  for (const a of addrs) {
    if (a.family === "IPv4" && !a.internal) return a.address;
  }
  return null;
}

/** When the harness URL names host.docker.internal, forward bridge:port -> 127.0.0.1:port. */
export function localForwardSpec(baseUrl: string): LocalForwardSpec | null {
  const norm = normalizeAnthropicBaseUrl(baseUrl);
  if (!norm) return null;
  const u = new URL(norm);
  if (u.hostname.toLowerCase() !== "host.docker.internal") return null;
  const bridge = dockerBridgeIpv4();
  if (!bridge) return null;
  const port = Number(u.port) || (u.protocol === "https:" ? 443 : 80);
  return { bindHost: bridge, port, targetHost: "127.0.0.1", targetPort: port };
}

let activeKey: string | null = null;
let activeServer: net.Server | null = null;

function pipe(client: net.Socket, target: net.Socket): void {
  client.pipe(target);
  target.pipe(client);
  const close = () => {
    client.destroy();
    target.destroy();
  };
  client.on("error", close);
  target.on("error", close);
  client.on("close", () => target.end());
  target.on("close", () => client.end());
}

export async function stopLocalStrataForward(): Promise<void> {
  const server = activeServer;
  activeServer = null;
  activeKey = null;
  if (!server) return;
  await new Promise<void>((resolve) => server.close(() => resolve()));
}

/** Start, replace, or stop the bridge forwarder for the active local harness lease. */
export async function syncLocalStrataForward(harness: LocalHarnessConfig | null): Promise<void> {
  const key = harness?.anthropic_base_url ?? "";
  if (key === activeKey) return;
  await stopLocalStrataForward();
  if (!harness) return;
  const spec = localForwardSpec(harness.anthropic_base_url);
  if (!spec) {
    activeKey = key;
    return;
  }
  const server = net.createServer((client) => {
    const target = net.connect({ host: spec.targetHost, port: spec.targetPort });
    target.on("connect", () => pipe(client, target));
    target.on("error", () => client.destroy());
  });
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen({ host: spec.bindHost, port: spec.port }, () => resolve());
  });
  activeServer = server;
  activeKey = key;
}
