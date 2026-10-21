import http from "node:http";
import { afterEach, describe, expect, it } from "vitest";
import { syncCustomHarnessBridge } from "./argus-custom-bridge.js";

describe("syncCustomHarnessBridge", () => {
  afterEach(async () => {
    await syncCustomHarnessBridge("test-group", null, null);
  });

  it("starts a loopback bridge and returns a container-facing URL", async () => {
    const upstream = http.createServer((_req, res) => {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ choices: [{ message: { content: "hi" } }] }));
    });
    await new Promise<void>((r) => upstream.listen(0, "127.0.0.1", () => r()));
    const port = (upstream.address() as { port: number }).port;
    const url = await syncCustomHarnessBridge(
      "test-group",
      {
        base_url: `http://127.0.0.1:${port}/api/v1`,
        label: "openrouter",
        model: "deepseek/deepseek-v4.1-flash-1017",
        provider: null,
      },
      "sk-bridge-test",
    );
    upstream.close();
    expect(url).toMatch(/^http:\/\/host\.docker\.internal:\d+$/);
  });
});
