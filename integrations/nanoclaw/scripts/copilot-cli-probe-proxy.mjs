import { execFileSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import tls from "node:tls";

const dir = "/tmp/cop-probe/ca";
const log = fs.createWriteStream("/tmp/cop-probe/requests.log", { flags: "a" });
const certs = {};
function ctx(host) {
  if (certs[host]) return certs[host];
  execFileSync("sh", [
    "-c",
    `cd ${dir} && openssl req -newkey rsa:2048 -nodes -keyout h.key -subj "/CN=${host}" -out h.csr 2>/dev/null && printf "subjectAltName=DNS:${host}" > ext && openssl x509 -req -in h.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 2 -extfile ext -out h.pem 2>/dev/null`,
  ]);
  return (certs[host] = tls.createSecureContext({
    key: fs.readFileSync(dir + "/h.key"),
    cert: fs.readFileSync(dir + "/h.pem"),
  }));
}
const redact = (v) => String(v).replace(/(github_pat_|gho_|ghu_|ghp_)[A-Za-z0-9_]*/g, "$1<fake>");
const inner = http.createServer((req, res) => {
  const host = req.headers.host;
  const h = {};
  for (const k of [
    "authorization",
    "user-agent",
    "content-type",
    "x-github-api-version",
    "copilot-integration-id",
    "editor-version",
  ])
    if (req.headers[k]) h[k] = redact(req.headers[k]);
  log.write(
    JSON.stringify({ host, method: req.method, path: req.url.replace(/\?.*/, "?<q>"), h }) + "\n",
  );
  if (req.url.startsWith("/copilot_internal/user") && process.env.MOCK === "1") {
    res.writeHead(200, { "content-type": "application/json" });
    return res.end(
      JSON.stringify({
        login: "fakeuser",
        access_type_sku: "free_limited_copilot",
        copilot_plan: "individual",
        chat_enabled: true,
        endpoints: {
          api: "https://api.individual.githubcopilot.com",
          proxy: "https://proxy.individual.githubcopilot.com",
          telemetry: "https://telemetry.individual.githubcopilot.com",
          "origin-tracker": "https://origin-tracker.individual.githubcopilot.com",
        },
      }),
    );
  }
  if (req.url.startsWith("/models") && process.env.MOCK === "1") {
    res.writeHead(200, { "content-type": "application/json" });
    return res.end(
      JSON.stringify({
        data: [
          {
            id: "gpt-5",
            name: "GPT-5",
            object: "model",
            vendor: "OpenAI",
            version: "gpt-5",
            model_picker_enabled: true,
            preview: false,
            capabilities: {
              family: "gpt-5",
              type: "chat",
              object: "model_capabilities",
              tokenizer: "o200k_base",
              limits: { max_context_window_tokens: 128000, max_output_tokens: 16000 },
              supports: { streaming: true, tool_calls: true },
            },
            supported_endpoints: (process.env.EP || "/chat/completions,/responses").split(","),
          },
        ],
      }),
    );
  }
  if (req.method === "POST") {
    res.writeHead(500, { "content-type": "application/json" });
    return res.end('{"error":{"message":"probe stop","code":"probe"}}');
  }
  res.writeHead(401, { "content-type": "application/json" });
  res.end('{"message":"Bad credentials"}');
});
const p = http.createServer((q, s) => {
  log.write(JSON.stringify({ plainhttp: q.url }) + "\n");
  s.writeHead(502);
  s.end();
});
p.on("connect", (req, sock, head) => {
  const [host] = req.url.split(":");
  log.write(JSON.stringify({ connect: req.url }) + "\n");
  sock.write("HTTP/1.1 200 OK\r\n\r\n");
  let c;
  try {
    c = ctx(host);
  } catch (e) {
    sock.destroy();
    return;
  }
  const t = new tls.TLSSocket(sock, { isServer: true, secureContext: c });
  t.on("error", (e) => log.write(JSON.stringify({ tlserr: host, e: e.code }) + "\n"));
  inner.emit("connection", t);
});
p.listen(18899, "127.0.0.1", () => console.log("up"));
