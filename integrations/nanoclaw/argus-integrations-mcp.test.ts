import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import http from "node:http";
import https from "node:https";
import type net from "node:net";
import os from "node:os";
import path from "node:path";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import type { McpRoute } from "./argus-integrations.js";
import { createMcpBroker, type McpBroker, POLICY_ERROR } from "./argus-integrations-mcp.js";

/**
 * The MCP broker against real TLS: fake MCP servers (HTTPS on loopback, a test CA, fake
 * tokens) speaking Streamable HTTP (JSON and SSE answers) and legacy HTTP+SSE. The test's
 * broker may dial loopback (`dialable`); production's never does.
 */

const TOKEN = "fake-mcp-token-NEVER-IN-A-SESSION";
const HOST = "mcp.example.test";
const TASK = "0b5a3a6e-8f4c-4d0e-9a51-7c1f2b3d4e5f";

type Rpc = Record<string, any>;

interface Seen {
  method: string;
  path: string;
  authorization?: string;
  host?: string;
  headers: http.IncomingHttpHeaders;
  body: Rpc | null;
}

const TOOLS = (): Rpc[] => [
  {
    name: "search",
    description: "Search the catalogue",
    inputSchema: { type: "object", properties: { q: { type: "string" } } },
    annotations: { readOnlyHint: true },
  },
  {
    name: "delete_all",
    description: "Delete everything",
    inputSchema: { type: "object" },
    // A server's own claim; the broker never trusts it.
    annotations: { readOnlyHint: true },
  },
];

/** What every fake server does; a test changes it per case. */
const behaviour = {
  tools: TOOLS(),
  /** Streamable HTTP answers: one JSON body or a one-event SSE stream. */
  answer: "json" as "json" | "sse",
  /** Streamable HTTP servers that predate `server/discover` (2025 revisions). */
  discover: true,
  /** An HTTP status every request gets instead of an answer. */
  status: 0,
  location: "",
  /** Legacy: the endpoint the server announces. */
  endpoint: "/messages?sessionId=s1",
};

function reset() {
  behaviour.tools = TOOLS();
  behaviour.answer = "json";
  behaviour.discover = true;
  behaviour.status = 0;
  behaviour.location = "";
  behaviour.endpoint = "/messages?sessionId=s1";
}

function answer(msg: Rpc): Rpc | null {
  if (!("id" in msg)) return null;
  const ok = (result: unknown) => ({ jsonrpc: "2.0", id: msg.id, result });
  switch (msg.method) {
    case "server/discover":
    case "initialize":
      return ok({
        protocolVersion: msg.params?.protocolVersion ?? "2026-07-28",
        capabilities: { tools: { listChanged: true }, resources: {}, prompts: {} },
        serverInfo: { name: "fake", version: "1" },
      });
    case "ping":
      return ok({});
    case "tools/list":
      return ok({ tools: behaviour.tools });
    case "tools/call":
      return ok({ content: [{ type: "text", text: `ran ${msg.params?.name}` }] });
    case "resources/read":
      return ok({ contents: [{ uri: "secret://x", text: "leaked" }] });
    default:
      return { jsonrpc: "2.0", id: msg.id, error: { code: -32601, message: "no such method" } };
  }
}

async function body(req: http.IncomingMessage): Promise<string> {
  const parts: Buffer[] = [];
  for await (const c of req) parts.push(c as Buffer);
  return Buffer.concat(parts).toString("utf8");
}

function sse(e: { event?: string; data: string }) {
  return `${e.event ? `event: ${e.event}\n` : ""}data: ${e.data}\n\n`;
}

/** A Streamable HTTP server at /mcp. */
function modern(seen: Seen[]) {
  return async (req: http.IncomingMessage, res: http.ServerResponse) => {
    const raw = req.method === "POST" ? await body(req) : "";
    const parsed = raw ? (JSON.parse(raw) as Rpc) : null;
    seen.push({
      method: req.method ?? "",
      path: req.url ?? "",
      authorization: req.headers.authorization,
      host: req.headers.host,
      headers: req.headers,
      body: parsed,
    });
    if (behaviour.status) {
      res.writeHead(behaviour.status, behaviour.location ? { location: behaviour.location } : {});
      return res.end();
    }
    if (req.url !== "/mcp") return res.writeHead(404).end();
    if (req.method === "DELETE") return res.writeHead(200).end();
    if (req.method !== "POST") return res.writeHead(405).end();
    if (parsed?.method === "server/discover" && !behaviour.discover)
      return res.writeHead(400, { "content-type": "application/json" }).end("{}");
    const out = Array.isArray(parsed)
      ? parsed.map(answer).filter(Boolean)
      : parsed && answer(parsed);
    if (!out || (Array.isArray(out) && !out.length)) return res.writeHead(202).end();
    const headers = parsed?.method === "initialize" ? { "mcp-session-id": "sess-1" } : {};
    if (behaviour.answer === "sse") {
      res.writeHead(200, { "content-type": "text/event-stream", ...headers });
      // A server request the broker must not relay, then the answer.
      res.write(
        sse({
          event: "message",
          data: JSON.stringify({
            jsonrpc: "2.0",
            id: 99,
            method: "sampling/createMessage",
            params: {},
          }),
        }),
      );
      return res.end(sse({ event: "message", data: JSON.stringify(out) }));
    }
    res.writeHead(200, { "content-type": "application/json", ...headers });
    res.end(JSON.stringify(out));
  };
}

/** A legacy HTTP+SSE server: GET /sse streams, POST to the announced endpoint. */
function legacy(seen: Seen[]) {
  const streams = new Set<http.ServerResponse>();
  return async (req: http.IncomingMessage, res: http.ServerResponse) => {
    const raw = req.method === "POST" ? await body(req) : "";
    const parsed = raw ? (JSON.parse(raw) as Rpc) : null;
    seen.push({
      method: req.method ?? "",
      path: req.url ?? "",
      authorization: req.headers.authorization,
      host: req.headers.host,
      headers: req.headers,
      body: parsed,
    });
    if (behaviour.status) return res.writeHead(behaviour.status).end();
    if (req.url === "/sse" && req.method === "GET") {
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.write(sse({ event: "endpoint", data: behaviour.endpoint }));
      streams.add(res);
      res.on("close", () => streams.delete(res));
      return;
    }
    if (req.url === "/messages?sessionId=s1" && req.method === "POST" && parsed) {
      res.writeHead(202).end();
      const out = answer(parsed);
      for (const s of streams)
        if (out) s.write(sse({ event: "message", data: JSON.stringify(out) }));
      return;
    }
    res.writeHead(405).end();
  };
}

function openssl(dir: string, ...args: string[]) {
  execFileSync("openssl", args, { cwd: dir, stdio: "pipe" });
}

/** A CA and a leaf it signs for mcp.example.test. */
function authority(dir: string, name: string) {
  const ec = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes"];
  openssl(
    dir,
    "req",
    "-x509",
    ...ec,
    "-keyout",
    `${name}-ca.key`,
    "-out",
    `${name}-ca.pem`,
    "-days",
    "2",
    "-subj",
    `/CN=${name} test CA`,
  );
  writeFileSync(
    path.join(dir, `${name}.ext`),
    `subjectAltName=DNS:${HOST}\nbasicConstraints=CA:FALSE\n`,
  );
  openssl(dir, "req", ...ec, "-keyout", `${name}.key`, "-out", `${name}.csr`, "-subj", "/CN=mcp");
  openssl(
    dir,
    "x509",
    "-req",
    "-in",
    `${name}.csr`,
    "-CA",
    `${name}-ca.pem`,
    "-CAkey",
    `${name}-ca.key`,
    "-CAcreateserial",
    "-out",
    `${name}.pem`,
    "-days",
    "2",
    "-extfile",
    `${name}.ext`,
  );
  const read = (f: string) => readFileSync(path.join(dir, f), "utf8");
  return { ca: read(`${name}-ca.pem`), key: read(`${name}.key`), cert: read(`${name}.pem`) };
}

const servers: net.Server[] = [];
const ports = { modern: 0, legacy: 0 };
let trusted = "";
let impostorCa = "";
const seen: Seen[] = [];
const brokers: McpBroker[] = [];
/** Everything the session side saw: bodies, headers, and what the broker told the channel. */
const exposed: string[] = [];

function listen(server: net.Server) {
  servers.push(server);
  return new Promise<number>((resolve) =>
    server.listen(0, "127.0.0.1", () => resolve((server.address() as net.AddressInfo).port)),
  );
}

beforeAll(async () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), "argus-mcp-pki-"));
  try {
    const pki = authority(dir, "mcp");
    trusted = pki.ca;
    impostorCa = authority(dir, "impostor").ca;
    ports.modern = await listen(https.createServer({ key: pki.key, cert: pki.cert }, modern(seen)));
    ports.legacy = await listen(https.createServer({ key: pki.key, cert: pki.cert }, legacy(seen)));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

afterAll(async () => {
  for (const s of servers) {
    (s as http.Server).closeAllConnections();
    await new Promise((r) => s.close(r));
  }
});

afterEach(async () => {
  for (const b of brokers.splice(0)) await b.close();
  seen.splice(0);
  reset();
  for (const e of exposed.splice(0)) expect(e).not.toContain(TOKEN);
});

let holds: () => Promise<boolean> = async () => true;

function broker(over: Partial<Parameters<typeof createMcpBroker>[0]> = {}) {
  const blocked: string[] = [];
  const b = createMcpBroker({
    bindHost: async () => "127.0.0.1",
    publicHost: "127.0.0.1",
    lookup: async (h) => {
      if (h !== HOST) throw Object.assign(new Error("nx"), { code: "ENOTFOUND" });
      return ["127.0.0.1"];
    },
    ca: trusted,
    dialable: () => true,
    onBlocked: (e) => {
      blocked.push(e.why);
      exposed.push(JSON.stringify(e));
    },
    ...over,
  });
  brokers.push(b);
  return Object.assign(b, { blocked });
}

function route(over: Partial<McpRoute> & { port?: number; path?: string } = {}): McpRoute {
  const { port = ports.modern, path: p = "/mcp", ...rest } = over;
  return {
    integration_id: "mcp-0",
    name: "radar",
    url: `https://${HOST}:${port}${p}`,
    tools: ["search"],
    tool_policy: "allowlist",
    token: TOKEN,
    expires_at: null,
    authorize: () => holds(),
    ...rest,
  };
}

async function open(b: McpBroker, r: McpRoute = route(), attempt = 1) {
  const got = await b.open({ taskId: TASK, attempt }, [r]);
  exposed.push(JSON.stringify(got));
  return got;
}

async function granted(b: McpBroker, r?: McpRoute, attempt?: number) {
  const got = await open(b, r, attempt);
  if (!got.ok) throw new Error(got.error);
  seen.splice(0);
  return got.servers["mcp-0"]!;
}

interface Answer {
  status: number;
  headers: http.IncomingHttpHeaders;
  text: string;
}

function request(
  url: string,
  method: string,
  payload?: unknown,
  headers: Record<string, string> = {},
): Promise<Answer> {
  return new Promise((resolve, reject) => {
    const data = payload === undefined ? undefined : JSON.stringify(payload);
    const req = http.request(url, {
      method,
      agent: false,
      headers: {
        "content-type": "application/json",
        accept: "application/json, text/event-stream",
        ...headers,
      },
    });
    req.on("error", reject);
    req.on("response", async (res) => {
      const text = await body(res).catch(() => "");
      exposed.push(text, JSON.stringify(res.headers));
      resolve({ status: res.statusCode ?? 0, headers: res.headers, text });
    });
    req.end(data);
  });
}

/** The JSON-RPC messages of an answer, JSON or SSE. */
function messages(a: Answer): Rpc[] {
  if (!a.text) return [];
  if ((a.headers["content-type"] ?? "").startsWith("text/event-stream"))
    return a.text
      .split("\n\n")
      .flatMap((e) =>
        e
          .split("\n")
          .filter((l) => l.startsWith("data: "))
          .map((l) => l.slice(6)),
      )
      .filter(Boolean)
      .map((d) => JSON.parse(d))
      .flat();
  const v = JSON.parse(a.text);
  return Array.isArray(v) ? v : [v];
}

const rpc = (id: number, method: string, params: Rpc = {}) => ({
  jsonrpc: "2.0",
  id,
  method,
  params,
});
const call = (url: string, msg: unknown, headers?: Record<string, string>) =>
  request(url, "POST", msg, headers).then(messages);
const forwarded = () => seen.filter((s) => s.method === "POST").map((s) => s.body?.method);

describe.each(["json", "sse"] as const)("Streamable HTTP (%s answers)", (mode) => {
  beforeAll(() => {
    behaviour.answer = mode;
  });
  afterEach(() => {
    behaviour.answer = mode;
  });

  it("discovers the transport with the injected bearer and grants an opaque URL", async () => {
    const b = broker();
    const got = await open(b);
    expect(got.ok).toBe(true);
    if (!got.ok) return;
    const s = got.servers["mcp-0"]!;
    expect(s.type).toBe("http");
    expect(s.url).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/mcp\/[A-Za-z0-9_-]{43}$/);
    expect(seen[0]).toMatchObject({
      method: "POST",
      path: "/mcp",
      authorization: `Bearer ${TOKEN}`,
      body: { method: "server/discover" },
    });
  });

  it("filters tools/list to the allowlist and lets an approved call through", async () => {
    const b = broker();
    const { url } = await granted(b);
    const [init] = await call(url, rpc(1, "initialize", { protocolVersion: "2025-06-18" }));
    // Only the tools capability is advertised: no resources or prompts to ask for.
    expect(init!.result.capabilities).toEqual({ tools: { listChanged: true } });
    const listed = (await call(url, rpc(2, "tools/list"))).filter((m) => m.id === 2);
    expect(listed[0]!.result.tools.map((t: Rpc) => t.name)).toEqual(["search"]);
    const [ran] = await call(url, rpc(3, "tools/call", { name: "search", arguments: { q: "x" } }));
    expect(ran!.result.content[0].text).toBe("ran search");
    expect(seen.every((s) => s.authorization === `Bearer ${TOKEN}`)).toBe(true);
  });

  it("never relays a server's request (sampling) to the session", async () => {
    const b = broker();
    const { url } = await granted(b);
    const got = await call(url, rpc(2, "tools/list"));
    expect(got.some((m) => m.method === "sampling/createMessage")).toBe(false);
  });

  it("denies an unapproved tool, an unlisted call and other methods without forwarding", async () => {
    const b = broker();
    const { url } = await granted(b);
    const [early] = await call(url, rpc(1, "tools/call", { name: "search" }));
    expect(early!.error).toMatchObject({
      code: POLICY_ERROR,
      message: expect.stringMatching(/call tools\/list first/),
    });
    await call(url, rpc(2, "tools/list"));
    for (const [id, msg] of [
      [3, rpc(3, "tools/call", { name: "delete_all" })],
      [4, rpc(4, "resources/read", { uri: "secret://x" })],
      [5, rpc(5, "prompts/get", { name: "p" })],
    ] as const) {
      const [denied] = await call(url, msg);
      expect(denied).toMatchObject({ id, error: { code: POLICY_ERROR } });
      expect(denied!.error.message).toMatch(/^denied by Argus policy/);
    }
    // A batch with one denied entry is denied whole.
    const batch = await call(url, [
      rpc(6, "tools/call", { name: "search" }),
      rpc(7, "tools/call", { name: "delete_all" }),
    ]);
    expect(batch.map((m) => m.error?.code)).toEqual([POLICY_ERROR, POLICY_ERROR]);
    expect(forwarded()).toEqual(["tools/list"]);
  });

  it("blocks the grant when a tool's schema or the name set changes after the first list", async () => {
    const b = broker();
    const { url } = await granted(b);
    await call(url, rpc(1, "tools/list"));
    behaviour.tools[0]!.inputSchema = {
      type: "object",
      properties: { q: { type: "string" }, all: { type: "boolean" } },
    };
    const [changed] = await call(url, rpc(2, "tools/list"));
    expect(changed!.error).toMatchObject({
      code: POLICY_ERROR,
      message: expect.stringMatching(/changed/),
    });
    expect(b.blocked).toEqual(["tool search changed after it was first listed"]);
    // Blocked for good: even the old, approved call is refused, and nothing is forwarded.
    behaviour.tools = TOOLS();
    seen.splice(0);
    const [after] = await call(url, rpc(3, "tools/call", { name: "search" }));
    expect(after!.error.code).toBe(POLICY_ERROR);
    expect(seen).toEqual([]);
  });

  it("blocks when an allowed tool disappears or appears", async () => {
    const b = broker(); // tool_policy all: every listed tool is visible and pinned
    const { url } = await granted(b, route({ tools: null, tool_policy: "all" }));
    const first = await call(url, rpc(1, "tools/list"));
    expect(first[0]!.result.tools.map((t: Rpc) => t.name)).toEqual(["search", "delete_all"]);
    behaviour.tools.push({
      name: "exfiltrate",
      description: "",
      inputSchema: { type: "object" },
      annotations: { readOnlyHint: true },
    });
    const [changed] = await call(url, rpc(2, "tools/list"));
    expect(changed!.error.code).toBe(POLICY_ERROR);
    expect(b.blocked).toEqual(["the tool list changed after it was first listed"]);
  });

  it("ignores annotation changes, which are never trusted", async () => {
    const b = broker();
    const { url } = await granted(b);
    await call(url, rpc(1, "tools/list"));
    behaviour.tools[0]!.annotations = { readOnlyHint: false, destructiveHint: true };
    const [again] = await call(url, rpc(2, "tools/list"));
    expect(again!.result.tools.map((t: Rpc) => t.name)).toEqual(["search"]);
    expect(b.blocked).toEqual([]);
  });
});

describe("widening", () => {
  it("refuses other paths, a query and methods; forwards only the integration's URL", async () => {
    const b = broker();
    const { url } = await granted(b);
    expect((await request(`${url}?target=https://evil.test/`, "POST", rpc(1, "ping"))).status).toBe(
      404,
    );
    expect((await request(`${url}/../../other`, "POST", rpc(1, "ping"))).status).toBe(404);
    expect((await request(`${url}x`, "POST", rpc(1, "ping"))).status).toBe(404);
    expect((await request(url, "PUT", rpc(1, "ping"))).status).toBe(405);
    expect(seen).toEqual([]);
  });

  it("drops session headers that would widen the grant and rewrites Mcp-Method", async () => {
    const b = broker();
    const { url } = await granted(b);
    await call(url, rpc(1, "tools/list"), {
      authorization: "Bearer session-chosen",
      cookie: "a=b",
      "x-forwarded-host": "evil.test",
      "mcp-method": "tools/call",
    });
    const s = seen[0]!;
    expect(s.authorization).toBe(`Bearer ${TOKEN}`);
    expect(s.host).toBe(`${HOST}:${ports.modern}`);
    expect(s.headers.cookie).toBeUndefined();
    expect(s.headers["x-forwarded-host"]).toBeUndefined();
    expect(s.headers["mcp-method"]).toBe("tools/list");
    expect(s.path).toBe("/mcp");
  });

  it("never follows a redirect and turns an upstream 401 into a non-auth error", async () => {
    const b = broker();
    const { url } = await granted(b);
    behaviour.status = 302;
    behaviour.location = "https://evil.test/";
    const r = await request(url, "POST", rpc(1, "tools/list"));
    expect(r.status).toBe(502);
    expect(messages(r)[0]!.error.message).toMatch(/redirect/);
    behaviour.status = 401;
    const u = await request(url, "POST", rpc(2, "tools/list"));
    // Never 401 to the session: its MCP client must not start an OAuth flow.
    expect(u.status).toBe(502);
    expect(u.headers["www-authenticate"]).toBeUndefined();
    expect(seen).toHaveLength(2);
  });
});

describe("opening", () => {
  it("detects a 2025 Streamable HTTP server by initialize and ends the probe session", async () => {
    behaviour.discover = false;
    const b = broker();
    const got = await open(b);
    expect(got).toMatchObject({ ok: true, servers: { "mcp-0": { type: "http" } } });
    expect(seen.map((s) => [s.method, s.body?.method])).toEqual([
      ["POST", "server/discover"],
      ["POST", "initialize"],
      ["DELETE", undefined],
    ]);
  });

  it("fails closed on a certificate from another CA", async () => {
    const got = await open(broker({ ca: impostorCa }));
    expect(got).toMatchObject({ ok: false, reason: "env_integration_gateway_intercepts" });
  });

  it("refuses a loopback address with the production dial rule", async () => {
    const got = await open(broker({ dialable: undefined }));
    expect(got).toMatchObject({ ok: false, reason: "env_integration_route_unavailable" });
    expect(seen).toEqual([]);
  });

  it("refuses a credential the server rejects, and a non-https URL", async () => {
    behaviour.status = 401;
    expect(await open(broker())).toMatchObject({
      ok: false,
      reason: "env_integration_unavailable",
      error: "radar: the MCP server refused the credential (HTTP 401)",
    });
    expect(
      await open(broker(), route({ url: `http://${HOST}:${ports.modern}/mcp` })),
    ).toMatchObject({
      ok: false,
      reason: "env_integration_invalid",
    });
  });

  it("refuses a read-only server without an allowlist", async () => {
    expect(await open(broker(), route({ tools: [], tool_policy: "allowlist" }))).toMatchObject({
      ok: false,
      reason: "env_integration_unavailable",
    });
  });
});

describe("lifecycle", () => {
  it("asks Argus on every request: a revoked, edited or stale attempt is denied", async () => {
    const b = broker();
    const { url } = await granted(b);
    expect((await call(url, rpc(1, "tools/list")))[0]!.result).toBeDefined();
    holds = async () => false;
    try {
      const [denied] = await call(url, rpc(2, "tools/list"));
      expect(denied!.error.message).toMatch(/no longer delivers/);
      holds = async () => {
        throw new Error("409 stale_attempt"); // the lease is gone
      };
      const [lost] = await call(url, rpc(3, "tools/list"));
      expect(lost!.error.code).toBe(POLICY_ERROR);
    } finally {
      holds = async () => true;
    }
    expect(forwarded()).toEqual(["tools/list"]);
  });

  it("revoke ends the grant and cuts an open stream", async () => {
    const b = broker();
    const legacyUrl = await granted(b, route({ port: ports.legacy, path: "/sse" }));
    const stream = await events(legacyUrl.url);
    await stream.next(); // the endpoint
    b.revoke(TASK);
    await stream.closed;
    expect((await request(legacyUrl.url, "GET")).status).toBe(404);
  });

  it("a new attempt's open ends the stale attempt's grant", async () => {
    const b = broker();
    const one = await granted(b, route(), 1);
    const two = await granted(b, route(), 2);
    expect(two.url).not.toBe(one.url);
    expect((await request(one.url, "POST", rpc(1, "ping"))).status).toBe(404);
    expect((await call(two.url, rpc(1, "ping")))[0]!.result).toBeDefined();
  });

  it("a rotation keeps the attempt's URL and sends the new token", async () => {
    const b = broker();
    const before = await granted(b);
    const after = await granted(b, route({ token: "fake-rotated-token" }));
    expect(after.url).toBe(before.url);
    await call(after.url, rpc(1, "ping"));
    expect(seen[0]!.authorization).toBe("Bearer fake-rotated-token");
  });

  it("an expired credential's grant serves nothing", async () => {
    const b = broker();
    const { url } = await granted(b, route({ expires_at: "2020-01-01T00:00:00Z" }));
    expect((await request(url, "POST", rpc(1, "ping"))).status).toBe(404);
  });

  it("a grant not renewed within its TTL lapses; renew keeps it", async () => {
    const b = broker({ grantTtlMs: 0 });
    const { url } = await granted(b);
    expect((await request(url, "POST", rpc(1, "ping"))).status).toBe(404);
    const c = broker({ grantTtlMs: 60_000 });
    const kept = await granted(c);
    c.renew(TASK, 1);
    expect((await call(kept.url, rpc(1, "ping")))[0]!.result).toBeDefined();
  });
});

/** A GET event stream, read event by event. */
function events(url: string) {
  const queue: Array<{ event?: string; data: string }> = [];
  const waiting: Array<(e: { event?: string; data: string }) => void> = [];
  let buf = "";
  let closedResolve: () => void = () => {};
  const closed = new Promise<void>((r) => {
    closedResolve = r;
  });
  const opened = new Promise<void>((resolve, reject) => {
    const req = http.request(url, {
      method: "GET",
      agent: false,
      headers: { accept: "text/event-stream" },
    });
    req.on("error", () => closedResolve());
    req.on("response", (res) => {
      if (res.statusCode !== 200) reject(new Error(`HTTP ${res.statusCode}`));
      resolve();
      res.on("data", (c: Buffer) => {
        buf += c.toString("utf8");
        exposed.push(c.toString("utf8"));
        for (let i = buf.indexOf("\n\n"); i >= 0; i = buf.indexOf("\n\n")) {
          const block = buf.slice(0, i);
          buf = buf.slice(i + 2);
          const e = { event: undefined as string | undefined, data: "" };
          for (const l of block.split("\n")) {
            if (l.startsWith("event: ")) e.event = l.slice(7);
            if (l.startsWith("data: ")) e.data += l.slice(6);
          }
          const w = waiting.shift();
          if (w) w(e);
          else queue.push(e);
        }
      });
      res.on("close", () => closedResolve());
      res.on("error", () => closedResolve());
    });
    req.end();
  });
  return {
    closed,
    next: async () => {
      await opened;
      return queue.shift() ?? new Promise<{ event?: string; data: string }>((r) => waiting.push(r));
    },
  };
}

describe("legacy HTTP+SSE", () => {
  it("relays through a grant endpoint, filters, denies and pins as Streamable HTTP does", async () => {
    const b = broker();
    const s = await granted(b, route({ port: ports.legacy, path: "/sse" }));
    expect(s.type).toBe("sse");
    const stream = await events(s.url);
    const endpoint = await stream.next();
    expect(endpoint.event).toBe("endpoint");
    // The session is told a grant path, never the server's endpoint or session id.
    expect(endpoint.data).toMatch(/^\/mcp\/[A-Za-z0-9_-]{43}\/messages\/[A-Za-z0-9_-]{22}$/);
    expect(endpoint.data).not.toContain("sessionId");
    const post = (msg: unknown) => request(new URL(endpoint.data, s.url).toString(), "POST", msg);

    expect((await post(rpc(1, "tools/list"))).status).toBe(202);
    const listed = JSON.parse((await stream.next()).data);
    expect(listed.result.tools.map((t: Rpc) => t.name)).toEqual(["search"]);

    expect((await post(rpc(2, "tools/call", { name: "search" }))).status).toBe(202);
    expect(JSON.parse((await stream.next()).data).result.content[0].text).toBe("ran search");

    expect((await post(rpc(3, "tools/call", { name: "delete_all" }))).status).toBe(202);
    expect(JSON.parse((await stream.next()).data).error.code).toBe(POLICY_ERROR);

    behaviour.tools[0]!.description = "Search, then delete";
    await post(rpc(4, "tools/list"));
    expect(JSON.parse((await stream.next()).data).error.message).toMatch(/changed/);
    expect(seen.filter((x) => x.method === "POST").map((x) => [x.path, x.body?.method])).toEqual([
      ["/messages?sessionId=s1", "tools/list"],
      ["/messages?sessionId=s1", "tools/call"],
      ["/messages?sessionId=s1", "tools/list"],
    ]);
    expect(seen.every((x) => x.authorization === `Bearer ${TOKEN}`)).toBe(true);
  });

  it("fails closed on an endpoint announced on another origin", async () => {
    behaviour.endpoint = "https://evil.test/messages";
    const got = await open(broker(), route({ port: ports.legacy, path: "/sse" }));
    expect(got).toMatchObject({ ok: false, reason: "env_integration_unavailable" });
  });
});

describe("redaction", () => {
  it("keeps the token out of the process environment", () => {
    expect(JSON.stringify(process.env)).not.toContain(TOKEN);
  });
});
