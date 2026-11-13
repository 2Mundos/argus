/**
 * The MCP broker of task integrations (integrations spec 3.6, "MCP broker"): an
 * Argus-owned HTTP endpoint in the channel process, the only way a session reaches an
 * MCP integration. The session's MCP config names a grant URL on this listener; the
 * broker holds the upstream token in memory and adds it to each request it forwards,
 * so the session never sees it.
 *
 * Each grant is minted after Argus delivered the credential for the attempt the channel
 * holds: a random secret in the URL path, bound to one task, attempt and integration,
 * forwarding only to the integration's own URL (or, for legacy HTTP+SSE, the
 * same-origin endpoint its server announced), dialled only at the addresses resolved
 * when it was granted, over TLS verified against the public roots, never following a
 * redirect. Every request asks Argus again (`authorize`) whether the attempt still holds
 * the integration at that version. Nothing the session sends (headers, query, other
 * paths or hosts) widens a grant.
 *
 * Policy is enforced here, never by the upstream server or its tool annotations: only
 * tool discovery and calls pass (resources, prompts, completion and sampling do not);
 * `tools/list` answers are filtered to the allowlist, `tools/call` must name a listed,
 * allowed tool, and the first list pins every tool's name and schemas: a later list
 * that differs blocks the grant for the rest of its life. A refusal is a JSON-RPC error.
 *
 * A revoked or expired grant cuts its in-flight requests and streams at once (a call
 * cut that way may still have run upstream). Failures name the integration and a fixed
 * code only, never a grant or a token.
 */
import { createHash, randomBytes } from "node:crypto";
import { lookup as dnsLookup } from "node:dns/promises";
import http from "node:http";
import https from "node:https";
import net from "node:net";
import { StringDecoder } from "node:string_decoder";
import tls from "node:tls";
import type {
  IntegrationFailureReason,
  McpOpenResult,
  McpRoute,
  McpServerEntry,
  McpTransport,
} from "./argus-integrations.js";
import { internalAddress, type Lookup } from "./argus-integrations-route.js";

/**
 * How long a grant lives unless renewed: each heartbeat recheck that keeps the install
 * renews it, so a channel that stops rechecking loses MCP access within this. Never past
 * the credential's own expiry.
 */
export const GRANT_TTL_MS = 15 * 60_000;
/** The longest one response stream (SSE) stays open; a revocation closes it sooner. */
export const STREAM_MAX_MS = 60 * 60_000;
export const MCP_PROBE_TIMEOUT_MS = 10_000;
const HEADERS_TIMEOUT_MS = 5 * 60_000;
const MAX_REQUEST = 1024 * 1024;
const MAX_MESSAGE = 8 * 1024 * 1024;
/** JSON-RPC error code of every policy refusal (the implementation-defined range). */
export const POLICY_ERROR = -32003;
const MODERN = "2026-07-28";
const LEGACY = "2025-03-26";

/** Client-to-server methods that pass; everything else is refused. */
const METHODS = new Set([
  "initialize",
  "notifications/initialized",
  "ping",
  "server/discover",
  "tools/list",
  "tools/call",
  "notifications/cancelled",
  "notifications/progress",
]);
const REQUEST_HEADERS = [
  "content-type",
  "accept",
  "mcp-protocol-version",
  "mcp-session-id",
  "last-event-id",
];
const RESPONSE_HEADERS = ["content-type", "mcp-session-id"];
const GRANT_PATH = /^\/mcp\/([A-Za-z0-9_-]{43})(?:\/messages\/([A-Za-z0-9_-]{22}))?$/;

type Rpc = Record<string, unknown>;

interface Grant {
  key: string;
  secret: string;
  taskId: string;
  attempt: number;
  route: McpRoute;
  transport: "http" | "sse";
  url: URL;
  addresses: string[];
  /** The credential's own expiry (ms), or Infinity. */
  credentialExpiry: number;
  expiresAt: number;
  timer: NodeJS.Timeout | undefined;
  /** Allowed tool names; null: every tool the server lists (`tool_policy` all). */
  allowed: Set<string> | null;
  /** The first list's tools, by name, as canonical JSON; and each page's names, by cursor. */
  tools: Map<string, string>;
  pages: Map<string, string>;
  blocked: string | null;
  /** Requests whose answer is a tools/list page: JSON id -> cursor. */
  lists: Map<string, string>;
  /** Server pings the session may answer. */
  pings: Set<string>;
  /** Legacy HTTP+SSE: our endpoint key -> the server's endpoint and the stream it came on. */
  endpoints: Map<string, { url: URL; stream: http.ServerResponse }>;
  live: Set<() => void>;
}

class McpRefusal extends Error {
  readonly reason: IntegrationFailureReason;
  constructor(reason: IntegrationFailureReason, message: string) {
    super(message);
    this.reason = reason;
  }
}

const errCode = (err: unknown) => {
  const c = (err as { code?: unknown } | null)?.code;
  return typeof c === "string" && /^[A-Z0-9_]{1,64}$/.test(c) ? c : "error";
};
const keyOf = (secret: string) => createHash("sha256").update(secret).digest("hex");
const idKey = (id: unknown) => JSON.stringify(id ?? null);
const isObject = (v: unknown): v is Rpc => typeof v === "object" && v !== null && !Array.isArray(v);
const systemLookup: Lookup = async (host) =>
  (await dnsLookup(host, { all: true, verbatim: true })).map((a) => a.address);

/** Canonical JSON (sorted keys): two lists with the same tools compare equal. */
function canonical(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(canonical).join(",")}]`;
  if (isObject(v))
    return `{${Object.keys(v)
      .sort()
      .map((k) => `${JSON.stringify(k)}:${canonical(v[k])}`)
      .join(",")}}`;
  return JSON.stringify(v ?? null);
}

/** What pins a tool: what the model sees of it. Annotations are not trusted, nor pinned. */
const fingerprint = (t: Rpc) =>
  canonical({
    name: t.name,
    title: t.title,
    description: t.description,
    inputSchema: t.inputSchema,
    outputSchema: t.outputSchema,
  });

function rpcError(id: unknown, message: string): Rpc {
  return { jsonrpc: "2.0", id: id ?? null, error: { code: POLICY_ERROR, message } };
}

/** The integration's URL, or null when it is not a plain https URL. */
function upstreamUrl(raw: string): URL | null {
  if (!URL.canParse(raw)) return null;
  const u = new URL(raw);
  if (u.protocol !== "https:" || !u.hostname || u.username || u.password || u.hash) return null;
  return u;
}

const sameOrigin = (a: URL, b: URL) =>
  a.protocol === b.protocol && a.hostname === b.hostname && a.port === b.port;

/** Server-sent events, parsed incrementally (WHATWG rules: any line ending, comments). */
class SseParser {
  private decoder = new StringDecoder("utf8");
  private buf = "";
  private event: { event?: string; id?: string; data: string[]; size: number } = {
    data: [],
    size: 0,
  };
  constructor(
    private readonly emit: (e: { event?: string; id?: string; data: string }) => void,
    private readonly limit: number,
  ) {}

  push(chunk: Buffer): void {
    this.buf += this.decoder.write(chunk);
    for (;;) {
      const m = /\r\n|\r|\n/.exec(this.buf);
      // A trailing \r may be the first half of \r\n: wait for the next chunk.
      if (!m || (m[0] === "\r" && m.index === this.buf.length - 1)) break;
      const line = this.buf.slice(0, m.index);
      this.buf = this.buf.slice(m.index + m[0].length);
      this.line(line);
    }
    if (this.buf.length + this.event.size > this.limit) throw new Error("event too large");
  }

  private line(line: string): void {
    if (line === "") {
      const e = this.event;
      this.event = { data: [], size: 0 };
      if (e.data.length) this.emit({ event: e.event, id: e.id, data: e.data.join("\n") });
      return;
    }
    if (line.startsWith(":")) return;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    this.event.size += value.length;
    if (field === "data") this.event.data.push(value);
    else if (field === "event") this.event.event = value;
    else if (field === "id" && !value.includes("\0")) this.event.id = value;
  }
}

function sseEvent(e: { event?: string; id?: string; data: string }): string {
  return `${e.event ? `event: ${e.event}\n` : ""}${e.id !== undefined ? `id: ${e.id}\n` : ""}${e.data
    .split("\n")
    .map((l) => `data: ${l}\n`)
    .join("")}\n`;
}

async function readBody(req: http.IncomingMessage, limit: number): Promise<Buffer | null> {
  const parts: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    size += (chunk as Buffer).length;
    if (size > limit) return null;
    parts.push(chunk as Buffer);
  }
  return Buffer.concat(parts);
}

export interface McpBrokerOptions {
  /** The address sessions reach this process at (the docker bridge); null: no broker. */
  bindHost: () => Promise<string | null>;
  /** A fixed port (for a host firewall rule), or 0 for any free port. */
  port?: number;
  /** The host name sessions use for the listener in their MCP config. */
  publicHost?: string;
  lookup?: Lookup;
  /** Trust roots for upstream TLS: the bundled public roots unless a test gives its own. */
  ca?: string | readonly string[];
  /** Addresses the broker may dial: never loopback or link-local, unless a test says so. */
  dialable?: (ip: string) => boolean;
  grantTtlMs?: number;
  streamMaxMs?: number;
  probeTimeoutMs?: number;
  /** Told when a grant is blocked (non-secret: the integration's name and why). */
  onBlocked?: (event: { taskId: string; name: string; why: string }) => void;
}

export interface McpBroker extends McpTransport {
  /** Stop listening and cut every grant. */
  close(): Promise<void>;
}

export function createMcpBroker(opts: McpBrokerOptions): McpBroker {
  const lookup = opts.lookup ?? systemLookup;
  const ca = (opts.ca ?? tls.rootCertificates) as string | string[];
  const dialable = opts.dialable ?? ((ip: string) => !internalAddress(ip));
  const grantTtlMs = opts.grantTtlMs ?? GRANT_TTL_MS;
  const streamMaxMs = opts.streamMaxMs ?? STREAM_MAX_MS;
  const probeTimeoutMs = opts.probeTimeoutMs ?? MCP_PROBE_TIMEOUT_MS;
  const publicHost = opts.publicHost ?? "host.docker.internal";
  const grants = new Map<string, Grant>();
  let server: http.Server | null = null;
  let listening: Promise<{ port: number }> | null = null;

  function drop(grant: Grant): void {
    if (grants.get(grant.key) === grant) grants.delete(grant.key);
    clearTimeout(grant.timer);
    for (const cut of [...grant.live]) cut();
    grant.live.clear();
    grant.endpoints.clear();
  }

  /** Live for another TTL from now, never past the credential's expiry. */
  function extend(grant: Grant): void {
    clearTimeout(grant.timer);
    grant.expiresAt = Math.min(Date.now() + grantTtlMs, grant.credentialExpiry);
    grant.timer = setTimeout(() => drop(grant), Math.max(0, grant.expiresAt - Date.now()));
    grant.timer.unref();
  }

  function revoke(taskId: string, attempt?: number): void {
    for (const g of [...grants.values()])
      if (g.taskId === taskId && (attempt === undefined || g.attempt === attempt)) drop(g);
  }

  function block(grant: Grant, why: string): void {
    if (grant.blocked) return;
    grant.blocked = why;
    for (const cut of [...grant.live]) cut();
    opts.onBlocked?.({ taskId: grant.taskId, name: grant.route.name, why });
  }

  /** One request upstream, to a pinned address with the grant's token; never redirected. */
  function upstream(
    grant: { url: URL; addresses: string[]; route: McpRoute },
    target: URL,
    method: string,
    headers: Record<string, string>,
    body?: Buffer,
  ): http.ClientRequest {
    const { addresses } = grant;
    const pinned = ((_host: string, o: { all?: boolean }, cb: (...a: unknown[]) => void) => {
      if (o?.all)
        cb(
          null,
          addresses.map((address) => ({ address, family: net.isIP(address) })),
        );
      else cb(null, addresses[0], net.isIP(addresses[0]));
    }) as unknown as typeof import("node:dns").lookup;
    const host = grant.url.hostname.replace(/^\[|\]$/g, "");
    return https.request({
      protocol: "https:",
      hostname: host,
      port: Number(grant.url.port) || 443,
      path: `${target.pathname}${target.search}`,
      method,
      lookup: net.isIP(host) ? undefined : pinned,
      ...(net.isIP(host) ? {} : { servername: host }),
      ca,
      rejectUnauthorized: true,
      agent: false,
      headers: {
        ...headers,
        host: grant.url.host,
        authorization: `Bearer ${grant.route.token}`,
        "user-agent": "argus-mcp-broker",
        ...(body ? { "content-length": String(body.length) } : {}),
      },
    });
  }

  /** Client-to-server policy: null when every message may pass, else why not. */
  function outbound(grant: Grant, msgs: unknown[]): string | null {
    if (!msgs.length) return "an empty batch";
    for (const m of msgs) {
      if (!isObject(m) || m.jsonrpc !== "2.0") return "not a JSON-RPC 2.0 message";
      if (typeof m.method !== "string") {
        // A response: only to a server ping (sampling, elicitation and roots never pass).
        if (!grant.pings.delete(idKey(m.id))) return "responses to server requests are not relayed";
        continue;
      }
      if (!METHODS.has(m.method)) return `method ${m.method} is not allowed`;
      if (m.method === "tools/call") {
        const name = isObject(m.params) ? m.params.name : undefined;
        if (typeof name !== "string") return "tools/call names no tool";
        if (grant.allowed && !grant.allowed.has(name))
          return `tool ${name} is not in this integration's allowlist`;
        if (!grant.tools.has(name)) return `tool ${name} was not listed; call tools/list first`;
      }
    }
    for (const m of msgs as Rpc[])
      if (m.method === "tools/list" && "id" in m) {
        const cursor = isObject(m.params) ? m.params.cursor : undefined;
        grant.lists.set(idKey(m.id), typeof cursor === "string" ? cursor : "");
      }
    return null;
  }

  /** Server-to-client: filter tool lists, pin them, drop server requests. Null: drop it. */
  function inbound(grant: Grant, msg: unknown): unknown {
    if (Array.isArray(msg)) {
      const kept = msg.map((m) => inbound(grant, m)).filter((m) => m !== null);
      return kept.length ? kept : null;
    }
    if (!isObject(msg)) return null;
    if (typeof msg.method === "string") {
      if (!("id" in msg)) return msg; // a notification
      if (msg.method !== "ping") return null; // sampling, elicitation, roots: not relayed
      grant.pings.add(idKey(msg.id));
      return msg;
    }
    const result = msg.result;
    if (!isObject(result)) return msg;
    const key = idKey(msg.id);
    const cursor = grant.lists.get(key);
    grant.lists.delete(key);
    let out: Rpc = result;
    if (isObject(result.capabilities)) {
      // Initialize or discover: advertise tools only, so the client asks for nothing else.
      const caps = result.capabilities;
      out = { ...out, capabilities: isObject(caps.tools) ? { tools: caps.tools } : {} };
    }
    if (Array.isArray(result.tools)) {
      const tools = result.tools.filter(
        (t): t is Rpc =>
          isObject(t) &&
          typeof t.name === "string" &&
          (grant.allowed === null || grant.allowed.has(t.name)),
      );
      for (const t of tools) {
        const was = grant.tools.get(t.name as string);
        if (was !== undefined && was !== fingerprint(t))
          throw new Blocked(`tool ${t.name as string} changed after it was first listed`);
      }
      const page = cursor ?? "";
      const names = canonical(tools.map((t) => t.name).sort());
      const had = grant.pages.get(page);
      if (had !== undefined && had !== names)
        throw new Blocked("the tool list changed after it was first listed");
      grant.pages.set(page, names);
      for (const t of tools) grant.tools.set(t.name as string, fingerprint(t));
      out = { ...out, tools };
    }
    return out === result ? msg : { ...msg, result: out };
  }

  async function handle(req: http.IncomingMessage, res: http.ServerResponse): Promise<void> {
    res.setHeader("Cache-Control", "no-store");
    const plain = (status: number) => {
      res.statusCode = status;
      res.end();
    };
    const m = GRANT_PATH.exec(req.url ?? "");
    const grant = m ? grants.get(keyOf(m[1])) : undefined;
    // An unknown grant is 404, never 401: the session must not start an OAuth flow.
    if (!m || !grant) return plain(404);
    if (grant.expiresAt <= Date.now()) {
      drop(grant);
      return plain(404);
    }
    const method = req.method ?? "";
    let target = grant.url;
    let legacyStream: http.ServerResponse | null = null;
    if (m[2]) {
      const ep = grant.transport === "sse" ? grant.endpoints.get(m[2]) : undefined;
      if (!ep) return plain(404);
      if (method !== "POST") return plain(405);
      target = ep.url;
      legacyStream = ep.stream;
    } else if (
      grant.transport === "sse" ? method !== "GET" : !["GET", "POST", "DELETE"].includes(method)
    )
      return plain(405);

    const reply = (status: number, msgs: Rpc[]) => {
      // Legacy HTTP+SSE answers on the event stream, not on the POST.
      if (legacyStream) {
        for (const r of msgs)
          legacyStream.write(sseEvent({ event: "message", data: JSON.stringify(r) }));
        return plain(202);
      }
      res.statusCode = status;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(msgs.length === 1 ? msgs[0] : msgs));
    };
    const refuse = (msgs: unknown[], why: string) => {
      const ids = msgs.filter((x) => isObject(x) && typeof x.method === "string" && "id" in x);
      if (!ids.length) return plain(403);
      return reply(
        200,
        ids.map((x) => rpcError((x as Rpc).id, `denied by Argus policy: ${why}`)),
      );
    };

    let body: Buffer | undefined;
    let msgs: unknown[] = [];
    let single: Rpc | null = null;
    if (method === "POST") {
      const raw = await readBody(req, MAX_REQUEST);
      if (!raw) return plain(413);
      let parsed: unknown;
      try {
        parsed = JSON.parse(raw.toString("utf8"));
      } catch {
        return reply(400, [
          { jsonrpc: "2.0", id: null, error: { code: -32700, message: "parse error" } },
        ]);
      }
      msgs = Array.isArray(parsed) ? parsed : [parsed];
      single = !Array.isArray(parsed) && isObject(parsed) ? parsed : null;
      body = Buffer.from(JSON.stringify(parsed));
    }
    if (grant.blocked) return refuse(msgs, grant.blocked);
    let allowed = false;
    try {
      allowed = await grant.route.authorize();
    } catch {}
    if (grants.get(grant.key) !== grant) return plain(404);
    if (!allowed) return refuse(msgs, "Argus no longer delivers this integration to the attempt");
    if (method === "POST") {
      const why = outbound(grant, msgs);
      if (why) return refuse(msgs, why);
    }

    const headers: Record<string, string> = {};
    for (const h of REQUEST_HEADERS) {
      const v = req.headers[h];
      if (typeof v === "string") headers[h] = v;
    }
    // The modern transport's routing header must say what the body says.
    if (
      typeof req.headers["mcp-method"] === "string" &&
      single &&
      typeof single.method === "string"
    )
      headers["mcp-method"] = single.method;
    const up = upstream(grant, target, method, headers, body);
    let done = false;
    const cut = () => {
      if (done) return;
      done = true;
      grant.live.delete(cut);
      up.destroy();
      res.destroy();
    };
    grant.live.add(cut);
    res.on("close", cut);
    const headersTimer = setTimeout(cut, HEADERS_TIMEOUT_MS);
    headersTimer.unref();
    up.on("error", () => {
      clearTimeout(headersTimer);
      if (!res.headersSent && !done)
        return reply(502, [rpcError(single?.id, "the MCP server is unreachable through Argus")]);
      cut();
    });
    up.on("response", (upRes) => {
      clearTimeout(headersTimer);
      void relay(grant, m[1], upRes, res, cut, single, reply).catch(() => cut());
    });
    up.end(body);
  }

  async function relay(
    grant: Grant,
    secret: string,
    upRes: http.IncomingMessage,
    res: http.ServerResponse,
    cut: () => void,
    single: Rpc | null,
    reply: (status: number, msgs: Rpc[]) => void,
  ): Promise<void> {
    const status = upRes.statusCode ?? 502;
    if ((status >= 300 && status < 400) || status === 401 || status === 403) {
      upRes.destroy();
      return reply(502, [
        rpcError(
          single?.id,
          status === 401 || status === 403
            ? `the MCP server refused the integration's credential (HTTP ${status}); tell the operator`
            : "the MCP server redirected; Argus does not follow redirects",
        ),
      ]);
    }
    for (const h of RESPONSE_HEADERS) {
      const v = upRes.headers[h];
      if (typeof v === "string") res.setHeader(h, v);
    }
    const type = (upRes.headers["content-type"] ?? "").split(";")[0].trim().toLowerCase();
    if (type === "application/json") {
      const raw = await readBody(upRes, MAX_MESSAGE);
      if (!raw) return cut();
      let out: unknown;
      try {
        out = inbound(grant, JSON.parse(raw.toString("utf8")));
      } catch (err) {
        if (!(err instanceof Blocked)) return cut();
        // Answer this request, then end every other one the grant has open.
        grant.live.delete(cut);
        reply(200, [rpcError(single?.id, `denied by Argus policy: ${err.message}`)]);
        return block(grant, err.message);
      }
      if (out === null) {
        res.statusCode = 202;
        return void res.end();
      }
      res.statusCode = status;
      return void res.end(JSON.stringify(out));
    }
    if (type !== "text/event-stream") {
      upRes.destroy();
      res.removeHeader("content-type");
      res.statusCode = status;
      return void res.end();
    }
    res.statusCode = status;
    res.flushHeaders();
    const streamTimer = setTimeout(cut, streamMaxMs);
    streamTimer.unref();
    const keys: string[] = [];
    res.on("close", () => {
      clearTimeout(streamTimer);
      for (const k of keys) grant.endpoints.delete(k);
    });
    const parser = new SseParser((e) => {
      if (e.event === "endpoint") {
        // Legacy HTTP+SSE: the server names where to POST. Same origin only, and the
        // session is told a grant path instead, so it never learns or picks the target.
        const to = URL.canParse(e.data, grant.url) ? new URL(e.data, grant.url) : null;
        if (grant.transport !== "sse" || !to || !sameOrigin(to, grant.url) || to.hash) {
          if (grant.transport === "sse")
            block(grant, "the server announced an endpoint on another origin");
          return cut();
        }
        const key = randomBytes(16).toString("base64url");
        grant.endpoints.set(key, { url: to, stream: res });
        keys.push(key);
        res.write(sseEvent({ event: "endpoint", data: `/mcp/${secret}/messages/${key}` }));
        return;
      }
      let out: unknown;
      try {
        out = inbound(grant, JSON.parse(e.data));
      } catch (err) {
        if (err instanceof Blocked) {
          const id = (JSON.parse(e.data) as Rpc).id;
          res.write(
            sseEvent({
              event: "message",
              data: JSON.stringify(rpcError(id, `denied by Argus policy: ${err.message}`)),
            }),
          );
          // Deliver the refusal on this stream, then end it and every other.
          grant.live.delete(cut);
          block(grant, err.message);
          upRes.destroy();
          res.end();
          return;
        }
        return cut();
      }
      if (out !== null)
        res.write(sseEvent({ event: e.event, id: e.id, data: JSON.stringify(out) }));
    }, MAX_MESSAGE);
    upRes.on("data", (chunk: Buffer) => {
      try {
        parser.push(chunk);
      } catch {
        cut();
      }
    });
    upRes.on("end", () => res.end());
    upRes.on("error", cut);
  }

  function listen(): Promise<{ port: number }> {
    listening ??= (async () => {
      const host = await opts.bindHost();
      if (!host)
        throw new McpRefusal(
          "env_integration_route_unavailable",
          "this worker has no docker bridge address for the MCP broker",
        );
      const port = opts.port ?? 0;
      if (!Number.isInteger(port) || port < 0 || port > 65535)
        throw new McpRefusal(
          "env_integration_route_unavailable",
          "ARGUS_INTEGRATION_MCP_PORT is not a port number",
        );
      const s = http.createServer((req, res) => {
        void handle(req, res).catch(() => res.destroy());
      });
      await new Promise<void>((resolve, reject) => {
        s.once("error", reject);
        s.listen({ host, port }, () => {
          s.off("error", reject);
          resolve();
        });
      });
      server = s;
      return { port: (s.address() as net.AddressInfo).port };
    })();
    return listening.catch((err) => {
      listening = null;
      throw err instanceof McpRefusal
        ? err
        : new McpRefusal(
            "env_integration_route_unavailable",
            `the MCP broker cannot listen (${errCode(err)})`,
          );
    });
  }

  /**
   * Which transport the server speaks, proving the route, TLS and the credential end to
   * end before the session is told about it: Streamable HTTP (discover, then initialize),
   * else legacy HTTP+SSE (a GET whose first event is a same-origin `endpoint`).
   */
  async function detect(g: {
    url: URL;
    addresses: string[];
    route: McpRoute;
  }): Promise<"http" | "sse"> {
    const deadline = AbortSignal.timeout(probeTimeoutMs);
    const name = g.route.name;
    const ask = (method: string, headers: Record<string, string>, body?: Rpc) =>
      new Promise<{ status: number; type: string; res: http.IncomingMessage }>(
        (resolve, reject) => {
          const data = body ? Buffer.from(JSON.stringify(body)) : undefined;
          const r = upstream(g, g.url, method, headers, data);
          const abort = () => r.destroy(new Error("timeout"));
          deadline.addEventListener("abort", abort, { once: true });
          r.on("error", (err) => {
            deadline.removeEventListener("abort", abort);
            reject(err);
          });
          r.on("response", (res) => {
            deadline.removeEventListener("abort", abort);
            const type = (res.headers["content-type"] ?? "").split(";")[0].trim().toLowerCase();
            resolve({ status: res.statusCode ?? 0, type, res });
          });
          r.end(data);
        },
      );
    const refuse = (status: number): never => {
      throw new McpRefusal(
        "env_integration_unavailable",
        status === 401 || status === 403
          ? `${name}: the MCP server refused the credential (HTTP ${status})`
          : status >= 300 && status < 400
            ? `${name}: the MCP server redirected (HTTP ${status}); redirects are not followed`
            : `${name}: the MCP server answered HTTP ${status}`,
      );
    };
    const json = {
      "content-type": "application/json",
      accept: "application/json, text/event-stream",
    };
    try {
      const discover = await ask(
        "POST",
        { ...json, "mcp-protocol-version": MODERN, "mcp-method": "server/discover" },
        { jsonrpc: "2.0", id: "argus-probe", method: "server/discover", params: {} },
      );
      discover.res.destroy();
      if (discover.status >= 200 && discover.status < 300) return "http";
      if (![400, 404, 405].includes(discover.status)) refuse(discover.status);
      const init = await ask("POST", json, {
        jsonrpc: "2.0",
        id: "argus-probe",
        method: "initialize",
        params: {
          protocolVersion: LEGACY,
          capabilities: {},
          clientInfo: { name: "argus-mcp-broker", version: "1" },
        },
      });
      init.res.destroy();
      const session = init.res.headers["mcp-session-id"];
      if (init.status >= 200 && init.status < 300) {
        // End the probe's session; a server that refuses is still Streamable HTTP.
        if (typeof session === "string")
          await ask("DELETE", { "mcp-session-id": session }).then(
            (d) => d.res.destroy(),
            () => {},
          );
        return "http";
      }
      if (![400, 404, 405].includes(init.status)) refuse(init.status);
      const stream = await ask("GET", { accept: "text/event-stream" });
      if (stream.status < 200 || stream.status >= 300) {
        stream.res.destroy();
        refuse(stream.status);
      }
      if (stream.type !== "text/event-stream") {
        stream.res.destroy();
        throw new McpRefusal(
          "env_integration_unavailable",
          `${name}: the server speaks neither Streamable HTTP nor HTTP+SSE`,
        );
      }
      const first = await new Promise<{ event?: string; data: string } | null>((resolve) => {
        const parser = new SseParser((e) => resolve(e), 64 * 1024);
        const abort = () => resolve(null);
        deadline.addEventListener("abort", abort, { once: true });
        stream.res.on("data", (c: Buffer) => {
          try {
            parser.push(c);
          } catch {
            resolve(null);
          }
        });
        stream.res.on("end", () => resolve(null));
        stream.res.on("error", () => resolve(null));
      });
      stream.res.destroy();
      if (
        first?.event !== "endpoint" ||
        !URL.canParse(first.data, g.url) ||
        !sameOrigin(new URL(first.data, g.url), g.url)
      )
        throw new McpRefusal(
          "env_integration_unavailable",
          `${name}: the HTTP+SSE server announced no same-origin endpoint`,
        );
      return "sse";
    } catch (err) {
      if (err instanceof McpRefusal) throw err;
      if ((err as Error).message === "timeout" || deadline.aborted)
        throw new McpRefusal(
          "env_integration_route_unavailable",
          `${name}: the MCP server did not answer within ${probeTimeoutMs} ms`,
        );
      const c = errCode(err);
      throw new McpRefusal(
        c.startsWith("ERR_TLS") || /CERT|UNABLE_TO_(VERIFY|GET_ISSUER)/.test(c)
          ? "env_integration_gateway_intercepts"
          : "env_integration_route_unavailable",
        `${name}: the MCP server is unreachable (${c})`,
      );
    }
  }

  async function grantOne(
    who: { taskId: string; attempt: number },
    route: McpRoute,
    port: number,
    was: Grant | undefined,
  ): Promise<McpServerEntry> {
    const url = upstreamUrl(route.url);
    if (!url)
      throw new McpRefusal("env_integration_invalid", `${route.name}: the url is not https`);
    const host = url.hostname.replace(/^\[|\]$/g, "");
    let addresses: string[];
    try {
      addresses = net.isIP(host) ? [host] : await lookup(host);
    } catch (err) {
      throw new McpRefusal(
        "env_integration_route_unavailable",
        `${route.name}: the server does not resolve (${errCode(err)})`,
      );
    }
    if (!addresses.length || !addresses.every(dialable))
      throw new McpRefusal(
        "env_integration_route_unavailable",
        `${route.name}: the server resolves to a loopback or link-local address`,
      );
    const transport = await detect({ url, addresses, route });
    // A running session keeps the URL and transport it started with (a rotation or an
    // edit re-grants under the same secret); a server that changed transport fails closed.
    if (was && was.transport !== transport)
      throw new McpRefusal(
        "env_integration_unavailable",
        `${route.name}: the MCP server changed transport during the attempt`,
      );
    const secret = was?.secret ?? randomBytes(32).toString("base64url");
    const expires = route.expires_at ? Date.parse(route.expires_at) : Number.NaN;
    const grant: Grant = {
      key: keyOf(secret),
      secret,
      taskId: who.taskId,
      attempt: who.attempt,
      route,
      transport,
      url,
      addresses,
      credentialExpiry: Number.isNaN(expires) ? Infinity : expires,
      expiresAt: 0,
      timer: undefined,
      allowed: route.tool_policy === "all" ? null : new Set(route.tools ?? []),
      tools: new Map(),
      pages: new Map(),
      blocked: null,
      lists: new Map(),
      pings: new Set(),
      endpoints: new Map(),
      live: new Set(),
    };
    extend(grant);
    grants.set(grant.key, grant);
    return { type: transport, url: `http://${publicHost}:${port}/mcp/${secret}` };
  }

  return {
    async open(who, routes): Promise<McpOpenResult> {
      const mine = new Map<string, Grant>();
      for (const g of [...grants.values()]) {
        if (g.taskId !== who.taskId) continue;
        // Another attempt's grants end now; this attempt's are replaced in place.
        if (g.attempt === who.attempt) mine.set(g.route.integration_id, g);
        drop(g);
      }
      try {
        const { port } = await listen();
        const servers: Record<string, McpServerEntry> = {};
        for (const route of routes) {
          if (route.tool_policy !== "all" && !route.tools?.length)
            throw new McpRefusal(
              "env_integration_unavailable",
              `${route.name}: a read-only MCP integration needs a tool allowlist`,
            );
          servers[route.integration_id] = await grantOne(
            who,
            route,
            port,
            mine.get(route.integration_id),
          );
        }
        return { ok: true, servers };
      } catch (err) {
        revoke(who.taskId);
        const e =
          err instanceof McpRefusal
            ? err
            : new McpRefusal(
                "env_integration_route_unavailable",
                `MCP broker error (${errCode(err)})`,
              );
        return { ok: false, reason: e.reason, error: e.message };
      }
    },
    revoke,
    renew(taskId, attempt) {
      for (const g of grants.values()) if (g.taskId === taskId && g.attempt === attempt) extend(g);
    },
    async close() {
      for (const g of [...grants.values()]) drop(g);
      const s = server;
      server = null;
      listening = null;
      if (s) {
        s.closeAllConnections();
        await new Promise<void>((resolve) => s.close(() => resolve()));
      }
    },
  };
}

class Blocked extends Error {}

/**
 * The production broker: listen on the docker bridge, which sessions reach as
 * `host.docker.internal`, on `ARGUS_INTEGRATION_MCP_PORT` or any free port. The
 * operator's environment sets the port only; nothing turns the checks off.
 */
export function nanoclawMcpBroker(
  env: NodeJS.ProcessEnv,
  onBlocked?: McpBrokerOptions["onBlocked"],
): McpBroker {
  const raw = env.ARGUS_INTEGRATION_MCP_PORT?.trim();
  return createMcpBroker({
    bindHost: async () => (await import("./argus-local-forward.js")).dockerBridgeIpv4(),
    port: raw ? (/^\d{1,5}$/.test(raw) ? Number(raw) : -1) : 0,
    onBlocked,
  });
}
