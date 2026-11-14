/**
 * The Kubernetes route of task integrations (integrations spec 3.6, "Route"): an
 * Argus-owned CONNECT broker in the channel process, the only way a session's kubectl
 * reaches a cluster. OneCLI v1.42.0 terminates TLS on every authenticated CONNECT, so
 * the gateway cannot carry a cluster-pinned kubeconfig; this route never terminates
 * TLS: it splices the bytes, and kubectl verifies the cluster's CA and hostname itself.
 *
 * Each grant is minted by the channel after Argus delivered the credential for the
 * attempt it holds, and lives only in this process's memory: a random secret (the
 * kubeconfig's `proxy-url` userinfo) bound to one task, attempt, integration and the
 * exact `host:port` of its server, dialled only at the addresses resolved when it was
 * granted. Every new tunnel asks Argus again (`authorize`) whether the attempt still
 * holds that integration at that version. Nothing a session sends (headers, flags,
 * environment) names a task or widens a grant; an unknown secret is `407`, any other
 * destination `403`. A revoked grant closes its tunnels at once; every tunnel also
 * closes after `TUNNEL_MAX_MS`, and kubectl reconnects through a fresh authorization.
 *
 * Before the kubeconfig is written, `open` proves the route: a CONNECT through the
 * listener with the grant and a TLS handshake verified against the integration's CA
 * (the bundled public roots when it has none, never the process's extra CAs) with
 * hostname checking. No token and no HTTP request is sent. Failures name the
 * integration and a fixed code only, never a grant or a token.
 */
import { createHash, randomBytes } from "node:crypto";
import { lookup as dnsLookup } from "node:dns/promises";
import net from "node:net";
import { networkInterfaces } from "node:os";
import tls from "node:tls";
// Types only: the kubectl e2e test loads these files with node's type stripping.
import type {
  IntegrationFailureReason,
  IntegrationRoute,
  IntegrationTransport,
  RouteResult,
} from "./argus-integrations.js";

export const ROUTE_PROBE_TIMEOUT_MS = 10_000;
/** The longest a tunnel lives; a revocation closes it sooner. */
export const TUNNEL_MAX_MS = 15 * 60_000;
const HEADER_TIMEOUT_MS = 10_000;
const DIAL_TIMEOUT_MS = 10_000;
const MAX_CONNECT_HEADER = 16 * 1024;
const USER = "argus";

/** Loopback, link-local (cloud metadata), unspecified, multicast or reserved: never dialled. */
const INTERNAL = new net.BlockList();
for (const [net4, bits] of [
  ["0.0.0.0", 8],
  ["127.0.0.0", 8],
  ["169.254.0.0", 16],
  ["224.0.0.0", 4],
  ["240.0.0.0", 4],
] as const)
  INTERNAL.addSubnet(net4, bits, "ipv4");
INTERNAL.addAddress("::", "ipv6");
INTERNAL.addAddress("::1", "ipv6");
INTERNAL.addSubnet("fe80::", 10, "ipv6");
INTERNAL.addSubnet("ff00::", 8, "ipv6");

/** As the API's `internal_address`: IPv4-mapped IPv6 forms match the IPv4 rules. */
export function internalAddress(ip: string): boolean {
  const family = net.isIP(ip);
  if (!family) return true;
  return INTERNAL.check(ip, family === 4 ? "ipv4" : "ipv6");
}

export type Lookup = (host: string) => Promise<string[]>;
const systemLookup: Lookup = async (host) =>
  (await dnsLookup(host, { all: true, verbatim: true })).map((a) => a.address);

interface Grant {
  key: string;
  taskId: string;
  attempt: number;
  /** `host:port` exactly as kubectl asks for it, lowercased. */
  authority: string;
  addresses: string[];
  port: number;
  authorize: () => Promise<boolean>;
  tunnels: Set<net.Socket>;
}

class RouteRefusal extends Error {
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
const bracket = (host: string) => (net.isIPv6(host) ? `[${host}]` : host);

/** The server's host (brackets stripped), port and CONNECT authority; null if unusable. */
function target(server: string): { host: string; port: number; authority: string } | null {
  if (!URL.canParse(server)) return null;
  const u = new URL(server);
  if (u.protocol !== "https:" || !u.hostname || u.username || u.password) return null;
  const host = u.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  const port = Number(u.port) || 443;
  return { host, port, authority: `${bracket(host)}:${port}` };
}

/** The secret from `Proxy-Authorization: Basic`, as Go sends a proxy URL's userinfo. */
function secretOf(headers: readonly string[]): string | null {
  const line = headers.find((h) => /^proxy-authorization:/i.test(h));
  const m = line && /^[^:]+:\s*Basic\s+([A-Za-z0-9+/=]+)\s*$/i.exec(line);
  if (!m) return null;
  const plain = Buffer.from(m[1], "base64").toString("utf8");
  const colon = plain.indexOf(":");
  return colon < 0 ? null : plain.slice(colon + 1) || null;
}

const STATUS_TEXT: Record<number, string> = {
  403: "Forbidden",
  405: "Method Not Allowed",
  407: "Proxy Authentication Required",
  502: "Bad Gateway",
};

function answer(sock: net.Socket, status: number): void {
  const auth = status === 407 ? `Proxy-Authenticate: Basic realm="${USER}"\r\n` : "";
  sock.end(
    `HTTP/1.1 ${status} ${STATUS_TEXT[status]}\r\n${auth}Connection: close\r\nContent-Length: 0\r\n\r\n`,
  );
}

/** The first pinned address that accepts within the dial timeout. */
async function dial(addresses: readonly string[], port: number): Promise<net.Socket | null> {
  for (const host of addresses) {
    const up = await new Promise<net.Socket | null>((resolve) => {
      const s = net.connect({ host, port, timeout: DIAL_TIMEOUT_MS });
      const fail = () => {
        s.destroy();
        resolve(null);
      };
      s.once("error", fail);
      s.once("timeout", fail);
      s.once("connect", () => {
        s.off("error", fail);
        s.off("timeout", fail);
        s.setTimeout(0);
        resolve(s);
      });
    });
    if (up) return up;
  }
  return null;
}

/** CONNECT through the route to `authority`; resolves the tunnel socket or a failure. */
function connectTunnel(
  listener: { host: string; port: number },
  authority: string,
  secret: string,
  signal: AbortSignal,
): Promise<net.Socket> {
  return new Promise((resolve, reject) => {
    const sock = net.connect({ host: listener.host, port: listener.port });
    const fail = (why: string) => {
      sock.destroy();
      reject(new Error(why));
    };
    const onAbort = () => fail("timeout");
    const onError = (err: Error) => fail(`the route is unreachable (${errCode(err)})`);
    const onClose = () => fail("the route closed the connection before answering CONNECT");
    signal.addEventListener("abort", onAbort, { once: true });
    sock.once("error", onError);
    sock.once("close", onClose);
    sock.once("connect", () => {
      const auth = Buffer.from(`${USER}:${secret}`).toString("base64");
      sock.write(
        `CONNECT ${authority} HTTP/1.1\r\nHost: ${authority}\r\nProxy-Authorization: Basic ${auth}\r\n\r\n`,
      );
    });
    let head = Buffer.alloc(0);
    const onData = (chunk: Buffer) => {
      head = Buffer.concat([head, chunk]);
      const end = head.indexOf("\r\n\r\n");
      if (end < 0) {
        if (head.length > MAX_CONNECT_HEADER) fail("the route sent an oversized CONNECT answer");
        return;
      }
      sock.off("data", onData);
      const status = /^HTTP\/1\.[01] (\d{3})/.exec(head.subarray(0, end).toString("latin1"));
      if (!status) return fail("the route sent no HTTP answer to CONNECT");
      if (status[1] !== "200") return fail(`the route refused the CONNECT (HTTP ${status[1]})`);
      // The server speaks first only after our ClientHello: anything already here is not TLS.
      if (head.length > end + 4) return fail("the route sent data before the TLS handshake");
      signal.removeEventListener("abort", onAbort);
      sock.off("error", onError);
      sock.off("close", onClose);
      resolve(sock);
    };
    sock.on("data", onData);
  });
}

/** A TLS handshake only, verified against the cluster's CA; nothing is written after it. */
function handshake(
  sock: net.Socket,
  host: string,
  ca: string | readonly string[],
  signal: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const t = tls.connect({
      socket: sock,
      host,
      // SNI carries a DNS name only; an IP server is still checked against the IP SAN.
      ...(net.isIP(host) ? {} : { servername: host }),
      ca: ca as string | string[],
      rejectUnauthorized: true,
    });
    const done = (err?: unknown) => {
      t.destroy();
      if (err === undefined) resolve();
      else reject(err);
    };
    signal.addEventListener("abort", () => done(new Error("timeout")), { once: true });
    t.once("secureConnect", () => done());
    t.once("error", (err) => done(err));
  });
}

/** Null when the grant reaches the cluster end to end through the listener, else why. */
async function probe(
  listener: { host: string; port: number },
  secret: string,
  route: IntegrationRoute,
  where: { host: string; authority: string },
  timeoutMs: number,
): Promise<RouteRefusal | null> {
  const ca = route.ca_data ? Buffer.from(route.ca_data, "base64").toString("utf8") : null;
  const signal = AbortSignal.timeout(timeoutMs);
  let sock: net.Socket;
  try {
    sock = await connectTunnel(listener, where.authority, secret, signal);
  } catch (err) {
    const why = (err as Error).message;
    return new RouteRefusal(
      "env_integration_route_unavailable",
      why === "timeout"
        ? `${route.name}: the route did not open a tunnel within ${timeoutMs} ms`
        : `${route.name}: ${why}`,
    );
  }
  try {
    // Never the process's extra CAs (NODE_EXTRA_CA_CERTS may hold the gateway's own).
    await handshake(sock, where.host, ca ?? tls.rootCertificates, signal);
    return null;
  } catch (err) {
    if ((err as Error).message === "timeout")
      return new RouteRefusal(
        "env_integration_route_unavailable",
        `${route.name}: no TLS handshake through the route within ${timeoutMs} ms`,
      );
    const c = errCode(err);
    return new RouteRefusal(
      "env_integration_gateway_intercepts",
      c === "ERR_TLS_CERT_ALTNAME_INVALID"
        ? `${route.name}: the certificate presented through the route does not name the server (${c}); something intercepts TLS`
        : `${route.name}: the certificate presented through the route is not trusted by the integration's CA (${c}); something intercepts TLS or the CA is wrong`,
    );
  }
}

export interface KubernetesRouteOptions {
  /** The address sessions reach this process at; null: no route. */
  bindHost: () => Promise<string | null>;
  /** A fixed port (for a host firewall rule), or 0 for any free port. */
  port?: number;
  lookup?: Lookup;
  tunnelMaxMs?: number;
  probeTimeoutMs?: number;
}

export interface KubernetesRoute extends IntegrationTransport {
  /** Stop listening and close every tunnel. */
  close(): Promise<void>;
}

export function createKubernetesRoute(opts: KubernetesRouteOptions): KubernetesRoute {
  const lookup = opts.lookup ?? systemLookup;
  const tunnelMaxMs = opts.tunnelMaxMs ?? TUNNEL_MAX_MS;
  const probeTimeoutMs = opts.probeTimeoutMs ?? ROUTE_PROBE_TIMEOUT_MS;
  const grants = new Map<string, Grant>();
  let server: net.Server | null = null;
  let listening: Promise<{ host: string; port: number }> | null = null;

  function drop(grant: Grant): void {
    grants.delete(grant.key);
    for (const s of grant.tunnels) s.destroy();
    grant.tunnels.clear();
  }

  function revoke(taskId: string, attempt?: number): void {
    for (const g of [...grants.values()])
      if (g.taskId === taskId && (attempt === undefined || g.attempt === attempt)) drop(g);
  }

  async function serve(sock: net.Socket, head: Buffer, end: number): Promise<void> {
    const [requestLine, ...headers] = head.subarray(0, end).toString("latin1").split("\r\n");
    const connect = /^CONNECT (\S+) HTTP\/1\.[01]$/.exec(requestLine);
    if (!connect) return answer(sock, 405);
    const secret = secretOf(headers);
    const grant = secret ? grants.get(keyOf(secret)) : undefined;
    if (!grant) return answer(sock, 407);
    if (connect[1].toLowerCase() !== grant.authority) return answer(sock, 403);
    let allowed = false;
    try {
      allowed = await grant.authorize();
    } catch {}
    if (!allowed || grants.get(grant.key) !== grant || sock.destroyed) return answer(sock, 403);
    const up = await dial(grant.addresses, grant.port);
    if (!up) return answer(sock, 502);
    if (grants.get(grant.key) !== grant || sock.destroyed) {
      up.destroy();
      return answer(sock, 403);
    }
    grant.tunnels.add(sock);
    const timer = setTimeout(() => sock.destroy(), tunnelMaxMs);
    timer.unref();
    const close = () => {
      clearTimeout(timer);
      grant.tunnels.delete(sock);
      sock.destroy();
      up.destroy();
    };
    sock.on("close", close);
    up.on("close", close);
    sock.on("error", close);
    up.on("error", close);
    sock.write("HTTP/1.1 200 Connection Established\r\n\r\n");
    const early = head.subarray(end + 4);
    if (early.length) up.write(early);
    sock.pipe(up);
    up.pipe(sock);
    sock.resume();
  }

  function accept(sock: net.Socket): void {
    const timer = setTimeout(() => sock.destroy(), HEADER_TIMEOUT_MS);
    timer.unref();
    sock.on("error", () => sock.destroy());
    sock.on("close", () => clearTimeout(timer));
    let head = Buffer.alloc(0);
    const onData = (chunk: Buffer) => {
      head = Buffer.concat([head, chunk]);
      const end = head.indexOf("\r\n\r\n");
      if (end < 0) {
        if (head.length > MAX_CONNECT_HEADER) sock.destroy();
        return;
      }
      sock.off("data", onData);
      sock.pause();
      clearTimeout(timer);
      void serve(sock, head, end).catch(() => sock.destroy());
    };
    sock.on("data", onData);
  }

  function listen(): Promise<{ host: string; port: number }> {
    listening ??= (async () => {
      const host = await opts.bindHost();
      if (!host)
        throw new RouteRefusal(
          "env_integration_route_unavailable",
          "this worker has no docker bridge address for the Kubernetes route",
        );
      const port = opts.port ?? 0;
      if (!Number.isInteger(port) || port < 0 || port > 65535)
        throw new RouteRefusal(
          "env_integration_route_unavailable",
          "ARGUS_INTEGRATION_ROUTE_PORT is not a port number",
        );
      const s = net.createServer(accept);
      await new Promise<void>((resolve, reject) => {
        s.once("error", reject);
        s.listen({ host, port }, () => {
          s.off("error", reject);
          resolve();
        });
      });
      server = s;
      return { host, port: (s.address() as net.AddressInfo).port };
    })();
    return listening.catch((err) => {
      listening = null;
      throw err instanceof RouteRefusal
        ? err
        : new RouteRefusal(
            "env_integration_route_unavailable",
            `the Kubernetes route cannot listen (${errCode(err)})`,
          );
    });
  }

  async function grantOne(
    who: { taskId: string; attempt: number },
    route: IntegrationRoute,
    listener: { host: string; port: number },
  ): Promise<string> {
    const where = target(route.server);
    if (!where)
      throw new RouteRefusal("env_integration_invalid", `${route.name}: the server is not a URL`);
    let addresses: string[];
    try {
      addresses = net.isIP(where.host) ? [where.host] : await lookup(where.host);
    } catch (err) {
      throw new RouteRefusal(
        "env_integration_route_unavailable",
        `${route.name}: the server does not resolve (${errCode(err)})`,
      );
    }
    if (!addresses.length || addresses.some(internalAddress))
      throw new RouteRefusal(
        "env_integration_route_unavailable",
        `${route.name}: the server resolves to a loopback or link-local address`,
      );
    const secret = randomBytes(32).toString("base64url");
    const grant: Grant = {
      key: keyOf(secret),
      taskId: who.taskId,
      attempt: who.attempt,
      authority: where.authority,
      addresses,
      port: where.port,
      authorize: route.authorize,
      tunnels: new Set(),
    };
    grants.set(grant.key, grant);
    const refused = await probe(listener, secret, route, where, probeTimeoutMs);
    if (refused) throw refused;
    return `http://${USER}:${secret}@${bracket(listener.host)}:${listener.port}`;
  }

  return {
    async open(who, routes): Promise<RouteResult> {
      // A new attempt (or a rotation) fences every earlier grant of the task first.
      revoke(who.taskId);
      try {
        const listener = await listen();
        const proxies: Record<string, string> = {};
        for (const route of routes)
          proxies[route.integration_id] = await grantOne(who, route, listener);
        return { ok: true, proxies };
      } catch (err) {
        revoke(who.taskId);
        const e =
          err instanceof RouteRefusal
            ? err
            : new RouteRefusal(
                "env_integration_route_unavailable",
                `route error (${errCode(err)})`,
              );
        return { ok: false, reason: e.reason, error: e.message };
      }
    },
    revoke,
    async close() {
      for (const g of [...grants.values()]) drop(g);
      const s = server;
      server = null;
      listening = null;
      if (s) await new Promise<void>((resolve) => s.close(() => resolve()));
    },
  };
}

export type Interfaces = typeof networkInterfaces;

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
const IFNAME = /^[A-Za-z0-9_.@-]{1,15}$/;

/**
 * The operator's `ARGUS_INTEGRATION_ROUTE_BIND` (an IPv4 literal or `if:<interface>`),
 * resolved against this process's interfaces when the route starts listening: the one
 * IPv4 address it names on a local non-internal interface, else null. A wildcard,
 * loopback, link-local, multicast or reserved address, IPv6, a host name, an unknown
 * interface, an address not assigned here, or an interface with several candidates
 * is null.
 */
export function routeBindAddress(
  spec: string,
  interfaces: Interfaces = networkInterfaces,
): string | null {
  const ifname = spec.startsWith("if:") ? spec.slice(3) : null;
  if (ifname === null ? !IPV4.test(spec) || internalAddress(spec) : !IFNAME.test(ifname))
    return null;
  const all = interfaces();
  const found = new Set<string>();
  for (const [name, addrs] of Object.entries(all)) {
    if (ifname !== null && name !== ifname) continue;
    for (const a of addrs ?? [])
      if (
        ["IPv4", "4"].includes(String(a.family)) &&
        !a.internal &&
        !internalAddress(a.address) &&
        (ifname !== null || a.address === spec)
      )
        found.add(a.address);
  }
  return found.size === 1 ? [...found][0] : null;
}

/**
 * The production route, on `ARGUS_INTEGRATION_ROUTE_PORT` or any free port. It listens
 * at `ARGUS_INTEGRATION_ROUTE_BIND` when the operator's host environment sets it (the
 * socket-mode topology, where sessions run on the outer daemon), with no fallback;
 * otherwise on the docker bridge, the address the session containers of this process
 * reach it at (as the local Strata forward does), or nowhere. Nothing a session or a
 * worker sends sets either; nothing turns the checks off.
 */
export function nanoclawKubernetesRoute(
  env: NodeJS.ProcessEnv,
  interfaces: Interfaces = networkInterfaces,
): KubernetesRoute {
  const raw = env.ARGUS_INTEGRATION_ROUTE_PORT?.trim();
  const bind = env.ARGUS_INTEGRATION_ROUTE_BIND;
  return createKubernetesRoute({
    bindHost:
      bind === undefined
        ? async () => (await import("./argus-local-forward.js")).dockerBridgeIpv4(interfaces)
        : async () => {
            const host = routeBindAddress(bind.trim(), interfaces);
            if (host) return host;
            throw new RouteRefusal(
              "env_integration_route_unavailable",
              "ARGUS_INTEGRATION_ROUTE_BIND does not name one IPv4 address of a local non-internal interface",
            );
          },
    port: raw ? (/^\d{1,5}$/.test(raw) ? Number(raw) : -1) : 0,
  });
}
