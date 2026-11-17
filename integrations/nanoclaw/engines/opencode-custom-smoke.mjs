#!/usr/bin/env node
/** Offline runtime smoke: real installed OpenCode, dummy gateway/provider, default permissions.
 * Run in a disposable agent container with this checkout read-only at /repo and
 * an empty writable directory at /smoke. No live account or gateway is contacted.
 */
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { policySnapshot } from "./model-policy.mjs";
import { OPENCODE_GATEWAY_SENTINEL, OPENCODE_GATEWAY_SETTINGS } from "./opencode-gateway.mjs";

const root = fs.mkdtempSync(path.join(process.env.TMPDIR || "/smoke", "opencode-smoke-"));
const work = path.join(root, "work");
const home = path.join(root, "home");
fs.mkdirSync(work);
fs.mkdirSync(home);
fs.writeFileSync(path.join(work, "fixture.txt"), "fixture-ok\n");
const cert = path.join(root, "dummy-ca.pem");
const key = path.join(root, "dummy-tls.key");
execFileSync(
  "openssl",
  [
    "req",
    "-x509",
    "-newkey",
    "rsa:2048",
    "-nodes",
    "-days",
    "1",
    "-subj",
    "/CN=openrouter.ai",
    "-addext",
    "subjectAltName=DNS:openrouter.ai",
    "-keyout",
    key,
    "-out",
    cert,
  ],
  { stdio: "ignore" },
);
const model = "deepseek/deepseek-v4.1-flash-1017";
const provider = { only: ["dummy-provider"], allow_fallbacks: false };
const settings = { ...OPENCODE_GATEWAY_SETTINGS, model, provider };
const policyFile = path.join(root, "policy.json");
fs.writeFileSync(
  policyFile,
  JSON.stringify(
    policySnapshot(
      JSON.parse(fs.readFileSync(new URL("./model-policy.defaults.json", import.meta.url), "utf8")),
      {
        task_id: "offline-smoke",
        attempt: 1,
        executor_account_provider: "custom",
        executor_settings: settings,
        executor_model: { executor: "opencode", model, account_provider: "custom" },
      },
    ),
  ),
);
let toolRequested = false;
let toolReturned = false;
let routingApplied = false;
let modelApplied = false;
let probeSeen = false;
let injections = 0;
const server = https.createServer(
  { key: fs.readFileSync(key), cert: fs.readFileSync(cert) },
  async (req, res) => {
    // Dummy gateway: exactly the production host/path scope, replacing a public sentinel
    // with another public dummy value. The child process never has the provider value.
    assert.equal(req.headers.host, "openrouter.ai");
    assert.ok(["/api/v1/models", "/api/v1/chat/completions"].includes(req.url));
    assert.equal(req.headers.authorization, `Bearer ${OPENCODE_GATEWAY_SENTINEL}`);
    req.headers.authorization = "Bearer dummy-smoke-key";
    injections++;
    assert.equal(req.headers.authorization, "Bearer dummy-smoke-key");
    if (req.url === "/api/v1/models") {
      probeSeen = true;
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ data: [{ id: model }] }));
      return;
    }
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString());
    modelApplied ||= body.model === model;
    routingApplied ||= JSON.stringify(body.provider) === JSON.stringify(provider);
    const hasRead = body.tools?.some((tool) => tool.function.name === "read");
    toolReturned ||= body.messages.some(
      (m) => m.role === "tool" && JSON.stringify(m.content).includes("fixture-ok"),
    );
    const callTool = hasRead && !toolRequested;
    toolRequested ||= callTool;
    const delta = callTool
      ? {
          tool_calls: [
            {
              index: 0,
              id: "call_fixture",
              type: "function",
              function: {
                name: "read",
                arguments: JSON.stringify({ filePath: path.join(work, "fixture.txt") }),
              },
            },
          ],
        }
      : { content: "fixture-ok" };
    const envelope = (delta, finish_reason) => ({
      id: "chatcmpl-smoke",
      object: "chat.completion.chunk",
      created: 1,
      model,
      choices: [{ index: 0, delta, finish_reason }],
    });
    if (body.stream) {
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.write(`data: ${JSON.stringify(envelope(delta, null))}\n\n`);
      res.write(`data: ${JSON.stringify(envelope({}, callTool ? "tool_calls" : "stop"))}\n\n`);
      res.end("data: [DONE]\n\n");
    } else {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(
        JSON.stringify({
          id: "chatcmpl-smoke",
          object: "chat.completion",
          created: 1,
          model,
          choices: [
            {
              index: 0,
              message: { role: "assistant", content: "fixture-ok" },
              finish_reason: "stop",
            },
          ],
          usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
        }),
      );
    }
  },
);
const proxy = http.createServer((_req, res) => res.writeHead(403).end());
const sockets = new Set();
proxy.on("connection", (socket) => {
  sockets.add(socket);
  socket.on("close", () => sockets.delete(socket));
  socket.on("error", () => socket.destroy());
});
proxy.on("connect", (req, socket, head) => {
  if (req.url !== "openrouter.ai:443") {
    socket.end("HTTP/1.1 403 Forbidden\r\n\r\n");
    return;
  }
  socket.write("HTTP/1.1 200 Connection Established\r\n\r\n");
  if (head.length) socket.unshift(head);
  server.emit("connection", socket);
});
await new Promise((resolve) => proxy.listen(0, "127.0.0.1", resolve));
const env = {
  PATH: process.env.PATH,
  HOME: home,
  TMPDIR: root,
  XDG_CONFIG_HOME: path.join(home, ".config"),
  XDG_DATA_HOME: path.join(home, ".local/share"),
  XDG_CACHE_HOME: path.join(home, ".cache"),
  ARGUS_DELEGATE_ROOTS: work,
  ARGUS_ACCOUNT_API_KEY: OPENCODE_GATEWAY_SENTINEL,
  ARGUS_MODEL_POLICY_FILE: policyFile,
  ARGUS_ACCOUNT_SETTINGS: JSON.stringify({ ...OPENCODE_GATEWAY_SETTINGS, model, provider }),
  HTTPS_PROXY: `http://127.0.0.1:${proxy.address().port}`,
  HTTP_PROXY: `http://127.0.0.1:${proxy.address().port}`,
  NODE_EXTRA_CA_CERTS: cert,
  SSL_CERT_FILE: cert,
};
async function run(args, prompt = "") {
  const delegate = fileURLToPath(new URL("./argus-delegate.mjs", import.meta.url));
  const child = spawn(process.execPath, [delegate, "opencode", ...args], {
    env,
    stdio: ["pipe", "pipe", "inherit"],
  });
  child.stdin.end(prompt);
  let output = "";
  child.stdout.on("data", (chunk) => {
    output += chunk;
  });
  const code = await new Promise((resolve) => child.on("close", resolve));
  const report = JSON.parse(output.trim().split("\n").at(-1));
  assert.equal(code, 0, JSON.stringify(report));
  return report;
}
try {
  const probe = await run(["--auth-probe"]);
  assert.equal(probe.provider, "accepted");
  assert.equal(probeSeen, true);
  const report = await run(
    ["--cwd", work, "--timeout", "60"],
    `Use the read tool to read ${path.join(work, "fixture.txt")}, then reply fixture-ok.`,
  );
  assert.equal(report.version, "1.18.27");
  assert.equal(toolRequested, true, "dummy provider must request a real installed tool");
  assert.equal(toolReturned, true, "OpenCode must return the actual fixture read result");
  assert.equal(modelApplied, true);
  assert.equal(routingApplied, true, "OpenRouter routing block must reach the wire");
  assert.equal(report.provider_block_applied, true);
  assert.match(report.tail, /fixture-ok/);
  assert.ok(!JSON.stringify(report).includes("dummy-smoke-key"));
  for (const artifact of report.artifacts)
    assert.ok(!fs.readFileSync(artifact, "utf8").includes("dummy-smoke-key"));
  console.log(
    JSON.stringify({
      ok: true,
      version: report.version,
      model: report.model,
      tool: "read",
      tool_result: "fixture-ok",
      routing_applied: routingApplied,
      probe: probe.provider,
      scoped_dummy_injections: injections,
      boundary: "offline mock gateway/provider; not a live lease or OneCLI authorization proof",
    }),
  );
} finally {
  for (const socket of sockets) socket.destroy();
  proxy.close();
  server.close();
}
