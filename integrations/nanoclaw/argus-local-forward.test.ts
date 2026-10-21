import net from "node:net";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as forward from "./argus-local-forward.js";
import {
  localForwardSpec,
  stopLocalStrataForward,
  syncLocalStrataForward,
} from "./argus-local-forward.js";
import { localHarnessFromCredential } from "./argus-local-harness.js";

afterEach(async () => {
  vi.restoreAllMocks();
  await stopLocalStrataForward();
});

describe("localForwardSpec", () => {
  it("maps host.docker.internal to the docker bridge and loopback", () => {
    vi.spyOn(forward, "dockerBridgeIpv4").mockReturnValue("172.17.0.1");
    expect(localForwardSpec("http://host.docker.internal:18080/")).toEqual({
      bindHost: "172.17.0.1",
      port: 18080,
      targetHost: "127.0.0.1",
      targetPort: 18080,
    });
  });
});

describe("syncLocalStrataForward", () => {
  it("forwards bridge traffic to a loopback listener", async () => {
    vi.spyOn(forward, "dockerBridgeIpv4").mockReturnValue("127.0.0.1");
    const backend = await new Promise<net.Server>((resolve) => {
      const s = net.createServer((socket) => {
        socket.end("ok");
      });
      s.listen(0, "127.0.0.1", () => resolve(s));
    });
    const port = (backend.address() as net.AddressInfo).port;
    const harness = localHarnessFromCredential({
      base_url: `http://host.docker.internal:${port}`,
    })!;
    await syncLocalStrataForward(harness);
    const reply = await new Promise<string>((resolve, reject) => {
      const client = net.connect({ host: "127.0.0.1", port }, () => undefined);
      let data = "";
      client.on("data", (chunk) => {
        data += chunk.toString();
      });
      client.on("end", () => resolve(data));
      client.on("error", reject);
    });
    expect(reply).toBe("ok");
    backend.close();
  });
});
