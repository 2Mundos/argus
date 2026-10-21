// Smoke server for the custom-harness bridge's container networking (tests and the
// deploy smoke; docs/specs/custom-harness.spec.md "Bridge reachable from agent
// containers"). Starts a fake OpenAI-compatible upstream on loopback and the real
// bridge on 0.0.0.0:PORT with key KEY, as the NanoClaw host does in a fleet container.
//   node bridge-smoke-server.mjs PORT KEY
import http from "node:http";
import { startAnthropicOpenAIBridge } from "./anthropic-openai-bridge.mjs";

const [port, key] = [Number(process.argv[2]), process.argv[3]];
const upstream = http.createServer((req, res) => {
  req.resume();
  res.writeHead(200, { "content-type": "application/json" });
  res.end(JSON.stringify({ model: "smoke", choices: [{ message: { content: "bridge-ok" } }] }));
});
await new Promise((r) => upstream.listen(0, "127.0.0.1", r));
await startAnthropicOpenAIBridge({
  settings: { base_url: `http://127.0.0.1:${upstream.address().port}/v1`, model: "smoke" },
  apiKey: "smoke-not-a-secret",
  listenHost: "0.0.0.0",
  listenPort: port,
  bridgeKey: key,
});
console.log(`bridge-smoke listening ${port}`);
