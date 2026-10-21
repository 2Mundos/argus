import http from "node:http";
import { afterEach, describe, expect, it } from "vitest";
import {
  BRIDGE_KEY_HEADER,
  bridgePlacement,
  parsePublishedPort,
  syncCustomHarnessBridge,
} from "./argus-custom-bridge.js";

const SETTINGS = (port: number) => ({
  base_url: `http://127.0.0.1:${port}/api/v1`,
  label: "openrouter",
  model: "deepseek/deepseek-v4.1-flash-1017",
  provider: null,
});

async function fakeUpstream(): Promise<{ port: number; close: () => void }> {
  const upstream = http.createServer((req, res) => {
    req.resume();
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ choices: [{ message: { content: "hi" } }] }));
  });
  await new Promise<void>((r) => upstream.listen(0, "127.0.0.1", () => r()));
  return { port: (upstream.address() as { port: number }).port, close: () => upstream.close() };
}

async function post(port: number, headers: Record<string, string> = {}): Promise<number> {
  const res = await fetch(`http://127.0.0.1:${port}/v1/messages`, {
    method: "POST",
    headers: { "content-type": "application/json", ...headers },
    body: JSON.stringify({
      model: "m",
      max_tokens: 8,
      messages: [{ role: "user", content: "hi" }],
    }),
  });
  await res.text();
  return res.status;
}

describe("syncCustomHarnessBridge", () => {
  afterEach(async () => {
    await syncCustomHarnessBridge("test-group", null, null);
  });

  it("on the Docker host: loopback bridge, container URL on its own port, key required", async () => {
    const up = await fakeUpstream();
    const ep = await syncCustomHarnessBridge(
      "test-group",
      SETTINGS(up.port),
      "sk-bridge-test",
      fetch,
      {
        env: {},
      },
    );
    expect(ep?.containerBaseUrl).toMatch(/^http:\/\/host\.docker\.internal:\d+$/);
    expect(ep?.bridgeKey).toMatch(/^[0-9a-f]{64}$/);
    const port = Number(new URL(ep!.containerBaseUrl).port);
    expect(await post(port)).toBe(401);
    expect(await post(port, { [BRIDGE_KEY_HEADER]: ep!.bridgeKey })).toBe(200);
    up.close();
  });

  it("in a fleet container: listens beyond loopback on the published port and names the host port", async () => {
    // The incident: the bridge listened on the NanoClaw container's loopback and the
    // URL named that port on host.docker.internal, which agent containers (another
    // network) cannot reach. Now it listens on 0.0.0.0:ARGUS_CUSTOM_BRIDGE_PORT and the
    // URL names the port Docker published for it.
    const up = await fakeUpstream();
    const probe = http.createServer();
    await new Promise<void>((r) => probe.listen(0, "127.0.0.1", () => r()));
    const containerPort = (probe.address() as { port: number }).port;
    await new Promise<void>((r) => probe.close(() => r()));
    const seen: Array<[string, number]> = [];
    const ep = await syncCustomHarnessBridge(
      "test-group",
      SETTINGS(up.port),
      "sk-bridge-test",
      fetch,
      {
        env: {
          ARGUS_CUSTOM_BRIDGE_PORT: String(containerPort),
          ARGUS_CUSTOM_BRIDGE_CONTAINER: "argus-i-1",
        },
        resolvePublished: async (container, port) => {
          seen.push([container, port]);
          return 32768;
        },
      },
    );
    expect(seen).toEqual([["argus-i-1", containerPort]]);
    expect(ep?.containerBaseUrl).toBe("http://host.docker.internal:32768");
    // Reachable on a non-loopback address of this machine, with the key.
    const { networkInterfaces } = await import("node:os");
    const external = Object.values(networkInterfaces())
      .flat()
      .find((a) => a && a.family === "IPv4" && !a.internal)?.address;
    if (external) {
      const res = await fetch(`http://${external}:${containerPort}/v1/messages`, {
        method: "POST",
        headers: { "content-type": "application/json", [BRIDGE_KEY_HEADER]: ep!.bridgeKey },
        body: JSON.stringify({
          model: "m",
          max_tokens: 8,
          messages: [{ role: "user", content: "x" }],
        }),
      });
      expect(res.status).toBe(200);
    }
    expect(await post(containerPort)).toBe(401);
    up.close();
  });

  it("does not leave a bridge running when the published port cannot be resolved", async () => {
    const up = await fakeUpstream();
    await expect(
      syncCustomHarnessBridge("test-group", SETTINGS(up.port), "sk-bridge-test", fetch, {
        env: { ARGUS_CUSTOM_BRIDGE_PORT: "3141", ARGUS_CUSTOM_BRIDGE_CONTAINER: "argus-i-1" },
        resolvePublished: async () => {
          throw new Error("no published port");
        },
      }),
    ).rejects.toThrow(/no published port/);
    // Port 3141 is free again: a second start can bind it.
    const s = http.createServer();
    await new Promise<void>((r, j) => {
      s.once("error", j);
      s.listen(3141, "0.0.0.0", () => r());
    });
    await new Promise<void>((r) => s.close(() => r()));
    up.close();
  });
});

describe("bridgePlacement / parsePublishedPort", () => {
  it("is host placement without the fleet env, published with it", () => {
    expect(bridgePlacement({})).toEqual({ kind: "host" });
    expect(
      bridgePlacement({
        ARGUS_CUSTOM_BRIDGE_PORT: "3141",
        ARGUS_CUSTOM_BRIDGE_CONTAINER: "argus-x",
      }),
    ).toEqual({ kind: "published", containerPort: 3141, container: "argus-x" });
    expect(() => bridgePlacement({ ARGUS_CUSTOM_BRIDGE_PORT: "3141" })).toThrow();
    expect(() =>
      bridgePlacement({ ARGUS_CUSTOM_BRIDGE_PORT: "99999", ARGUS_CUSTOM_BRIDGE_CONTAINER: "a" }),
    ).toThrow();
  });

  it("reads the host port from docker port output", () => {
    expect(parsePublishedPort("172.17.0.1:32768\n")).toBe(32768);
    expect(parsePublishedPort("[::]:40001\n0.0.0.0:40001\n")).toBe(40001);
    expect(parsePublishedPort("")).toBeNull();
  });
});

describe("published placement port contention", () => {
  afterEach(async () => {
    await syncCustomHarnessBridge("g1", null, null);
    await syncCustomHarnessBridge("g2", null, null);
  });

  it("a second custom group on the same NanoClaw container fails with a clear error", async () => {
    const up = await fakeUpstream();
    const env = { ARGUS_CUSTOM_BRIDGE_PORT: "3142", ARGUS_CUSTOM_BRIDGE_CONTAINER: "argus-i-1" };
    const resolvePublished = async () => 32769;
    await syncCustomHarnessBridge("g1", SETTINGS(up.port), "k", fetch, { env, resolvePublished });
    await expect(
      syncCustomHarnessBridge("g2", SETTINGS(up.port), "k", fetch, { env, resolvePublished }),
    ).rejects.toThrow(/one custom harness per NanoClaw container/);
    up.close();
  });
});
