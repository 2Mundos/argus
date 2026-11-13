/**
 * The gateway route probe for task Kubernetes integrations (integrations spec 3.6,
 * "Gateway path"): before a kubeconfig is installed, prove that the attempt's session
 * would reach each cluster end to end through its own gateway proxy.
 *
 * The probe sends what the session would send (an HTTP CONNECT with the session's
 * Proxy-Authorization) and then only a TLS handshake, verified against the
 * integration's CA (the bundled public roots when it has none, never the gateway's CA)
 * with hostname checking on. No token and no HTTP request is ever sent. A gateway that
 * terminates TLS itself (OneCLI v1.42.0 does for every authenticated CONNECT), refuses
 * the CONNECT or cannot be reached fails the probe: the attempt blocks with
 * `env_integration_gateway_intercepts` instead of handing the model a kubeconfig whose
 * token the gateway would see in plaintext.
 *
 * Nothing here logs or returns the proxy URL or its credentials: failures name the
 * integration and a fixed code only.
 */
import { randomBytes } from "node:crypto";
import net from "node:net";
import tls from "node:tls";
// Types only: the kubectl e2e test loads these files with node's type stripping.
import type { IntegrationRoute, RouteProbe } from "./argus-integrations.js";

export const ROUTE_PROBE_TIMEOUT_MS = 10_000;
const MAX_CONNECT_HEADER = 16 * 1024;

/** The proxy the attempt's session would get, held until `release`. */
export interface GatewayProxy {
  /** The session's HTTPS_PROXY (it may carry the agent token as userinfo); null if none. */
  proxy: string | null;
  release(): Promise<void>;
}

interface ProxyAddress {
  host: string;
  port: number;
  authorization: string | null;
}

function proxyAddress(raw: string): ProxyAddress | null {
  let u: URL;
  let user: string;
  let pass: string;
  try {
    u = new URL(raw);
    user = decodeURIComponent(u.username);
    pass = decodeURIComponent(u.password);
  } catch {
    return null;
  }
  if (u.protocol !== "http:" || !u.hostname) return null;
  return {
    host: u.hostname.replace(/^\[|\]$/g, ""),
    port: Number(u.port) || 80,
    // OneCLI reads the agent token from Basic Proxy-Authorization, as curl sends userinfo.
    authorization:
      user || pass ? `Basic ${Buffer.from(`${user}:${pass}`).toString("base64")}` : null,
  };
}

const errCode = (err: unknown) => {
  const c = (err as { code?: unknown } | null)?.code;
  return typeof c === "string" && /^[A-Z0-9_]{1,64}$/.test(c) ? c : "error";
};

/** CONNECT through `proxy` to `host:port`; resolves the tunnel socket or a failure. */
function connectTunnel(
  proxy: ProxyAddress,
  host: string,
  port: number,
  signal: AbortSignal,
): Promise<net.Socket> {
  return new Promise((resolve, reject) => {
    const sock = net.connect({ host: proxy.host, port: proxy.port });
    const fail = (why: string) => {
      sock.destroy();
      reject(new Error(why));
    };
    const onAbort = () => fail("timeout");
    const onError = (err: Error) => fail(`the gateway proxy is unreachable (${errCode(err)})`);
    const onClose = () => fail("the gateway closed the connection before answering CONNECT");
    signal.addEventListener("abort", onAbort, { once: true });
    sock.once("error", onError);
    sock.once("close", onClose);
    sock.once("connect", () => {
      const authority = net.isIPv6(host) ? `[${host}]:${port}` : `${host}:${port}`;
      sock.write(
        `CONNECT ${authority} HTTP/1.1\r\nHost: ${authority}\r\n` +
          `${proxy.authorization ? `Proxy-Authorization: ${proxy.authorization}\r\n` : ""}\r\n`,
      );
    });
    let head = Buffer.alloc(0);
    const onData = (chunk: Buffer) => {
      head = Buffer.concat([head, chunk]);
      const end = head.indexOf("\r\n\r\n");
      if (end < 0) {
        if (head.length > MAX_CONNECT_HEADER) fail("the gateway sent an oversized CONNECT answer");
        return;
      }
      sock.off("data", onData);
      const status = /^HTTP\/1\.[01] (\d{3})/.exec(head.subarray(0, end).toString("latin1"));
      if (!status) return fail("the gateway sent no HTTP answer to CONNECT");
      if (status[1] !== "200") return fail(`the gateway refused the CONNECT (HTTP ${status[1]})`);
      // The server speaks first only after our ClientHello: anything already here is not TLS.
      if (head.length > end + 4) return fail("the gateway sent data before the TLS handshake");
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

/** Null when `route` is reached end to end through `proxy`, else a non-secret reason. */
async function probeRoute(
  proxy: ProxyAddress,
  route: IntegrationRoute,
  timeoutMs: number,
): Promise<string | null> {
  let server: URL;
  try {
    server = new URL(route.server);
  } catch {
    return `${route.name}: the server is not a URL`;
  }
  const host = server.hostname.replace(/^\[|\]$/g, "");
  const port = Number(server.port) || 443;
  const ca = route.ca_data ? Buffer.from(route.ca_data, "base64").toString("utf8") : null;
  const signal = AbortSignal.timeout(timeoutMs);
  let sock: net.Socket;
  try {
    sock = await connectTunnel(proxy, host, port, signal);
  } catch (err) {
    const why = (err as Error).message;
    return why === "timeout"
      ? `${route.name}: the gateway did not open a tunnel within ${timeoutMs} ms`
      : `${route.name}: ${why}`;
  }
  try {
    // Never the process's extra CAs (NODE_EXTRA_CA_CERTS may hold the gateway's own).
    await handshake(sock, host, ca ?? tls.rootCertificates, signal);
    return null;
  } catch (err) {
    if ((err as Error).message === "timeout")
      return `${route.name}: no TLS handshake through the gateway within ${timeoutMs} ms`;
    const c = errCode(err);
    return c === "ERR_TLS_CERT_ALTNAME_INVALID"
      ? `${route.name}: the certificate presented through the gateway does not name the server (${c}); the gateway intercepts TLS`
      : `${route.name}: the certificate presented through the gateway is not trusted by the integration's CA (${c}); the gateway intercepts TLS`;
  }
}

/**
 * The probe the channel runs: take the session's proxy, check every route through it,
 * release it. No proxy configured fails closed: the session would have no gateway path.
 */
export function gatewayRouteProbe(
  gatewayProxy: (who: { taskId: string; attempt: number }) => Promise<GatewayProxy>,
  timeoutMs = ROUTE_PROBE_TIMEOUT_MS,
): RouteProbe {
  return async (routes, who) => {
    let held: GatewayProxy;
    try {
      held = await gatewayProxy(who);
    } catch (err) {
      return `the gateway lease for the probe failed (${errCode(err)})`;
    }
    try {
      if (!held.proxy) return "no gateway proxy is configured for this worker's sessions";
      const proxy = proxyAddress(held.proxy);
      if (!proxy) return "the session's gateway proxy is not a plain http URL";
      for (const route of routes) {
        const why = await probeRoute(proxy, route, timeoutMs);
        if (why) return why;
      }
      return null;
    } finally {
      await held.release().catch(() => undefined);
    }
  };
}

/**
 * The production gateway proxy: a short lease of the Argus agent group through NanoClaw's
 * own `ensureGatewaySession`, as the repository preflight takes one (argus-repo-preflight.ts),
 * so the probe carries the same OneCLI agent identity a worker session of this group gets.
 * No container starts. The container's `host.docker.internal` is the docker bridge here.
 */
export function nanoclawGatewayProxy(
  resolveGroup: () => Promise<{ id: string; name: string } | null>,
): (who: { taskId: string; attempt: number }) => Promise<GatewayProxy> {
  return async () => {
    const [runner, lifecycle, config, drivers, forward] = await Promise.all([
      import("../container-runner.js"),
      import("../gateway-session-lifecycle.js"),
      import("../config.js"),
      import("../drivers/index.js"),
      import("./argus-local-forward.js"),
    ]);
    if (typeof runner.ensureGatewaySession !== "function")
      throw new Error(
        "container-runner does not export ensureGatewaySession: run install.sh and rebuild NanoClaw",
      );
    const group = await resolveGroup();
    if (!group) throw new Error("no Argus agent group for the integration route probe");
    const sessionId = `preflight-${randomBytes(6).toString("hex")}`;
    const key = { installSlug: config.INSTALL_SLUG, agentGroupId: group.id, sessionId };
    const control = (await runner.ensureGatewaySession({
      key,
      disposition: "create",
      runtimeIdentity: `${key.installSlug}/${group.id}/${sessionId}`,
      groupName: group.name,
      containerName: `nanoclaw-v2-preflight-${sessionId.slice(10)}`,
      capabilities: drivers.getSessionDriver().capabilities(),
    } as never)) as {
      lease: { contribution: { env?: Record<string, string> } };
      controller: AbortController;
    };
    const env = control.lease.contribution.env ?? {};
    let proxy = env.HTTPS_PROXY || env.https_proxy || null;
    const u = proxy && URL.canParse(proxy) ? new URL(proxy) : null;
    const bridge = forward.dockerBridgeIpv4();
    if (u?.hostname.toLowerCase() === "host.docker.internal" && bridge) {
      u.hostname = bridge;
      proxy = u.href;
    }
    return {
      proxy,
      release: () =>
        lifecycle.releaseGatewaySession(
          control as never,
          { kind: "session-ended", reason: "integration-route-probe-done" } as never,
        ),
    };
  };
}
