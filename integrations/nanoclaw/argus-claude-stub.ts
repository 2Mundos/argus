/**
 * Hand the gateway's Claude sentinel to `argus-delegate` across Claude Code's
 * tool-subprocess boundary.
 *
 * Claude Code removes `CLAUDE_CODE_OAUTH_TOKEN` (the variable OneCLI uses for a
 * subscription account) from the environment of every process its Bash tool
 * starts, whatever the value; `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` are
 * not removed. The primary Claude process still sends the sentinel, but the
 * delegate, started from that tool, sees no credential. The real token never
 * enters the container (the gateway swaps it on the wire), so only the sentinel
 * needs to cross.
 *
 * The trusted host, which already holds OneCLI's container config for this agent
 * group, writes a read-only stub file with the sentinel, the gateway's proxy
 * address and the CA path. It does so only when OneCLI itself issued a sentinel
 * for this group (an Anthropic account is assigned) and the value cannot be a
 * real credential. `argus-delegate` restores the variable for its own `claude`
 * child only when the stub, the proxy and the CA all agree (claude spec in
 * docs/specs/account-selection.spec.md). Without an assignment there is no stub,
 * and the delegate keeps failing closed with `auth_missing`.
 */
import type { ContainerConfig } from "@onecli-sh/sdk";

export const CLAUDE_GATEWAY_STUB_PATH = "/home/node/.config/argus/claude-gateway.json";
export const CLAUDE_OAUTH_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN";

const MARKER = /placeholder|sentinel|managed|gateway|onecli/i;

/**
 * True when `value` can be a gateway placeholder and is not shaped like a real
 * Anthropic credential: a bounded opaque string; a `sk-ant-` value or a long one
 * must name itself as a placeholder. Mirrors `isGatewaySentinel` in argus-delegate.mjs.
 */
export function isGatewaySentinel(value: unknown): value is string {
  if (typeof value !== "string") return false;
  if (value.length < 8 || value.length > 256) return false;
  if (!/^[A-Za-z0-9._~+/=-]+$/.test(value)) return false;
  if (value.startsWith("sk-ant-") && !MARKER.test(value)) return false;
  if (value.length > 120 && !MARKER.test(value)) return false;
  return true;
}

/** `host:port` of a proxy URL, without its credentials; null when not an http(s) proxy. */
export function proxyAddress(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  try {
    const url = new URL(raw);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    if (!url.hostname) return null;
    const port = url.port || (url.protocol === "https:" ? "443" : "80");
    return `${url.hostname.toLowerCase()}:${port}`;
  } catch {
    return null;
  }
}

export interface ClaudeGatewayStub {
  version: 1;
  provider: "anthropic";
  variable: typeof CLAUDE_OAUTH_VARIABLE;
  sentinel: string;
  proxy: string;
  ca: string;
}

/**
 * The stub for `config`, or null. Needs, all from OneCLI's own answer for this
 * agent group: a Claude subscription sentinel, the proxy address and the CA path.
 * Nothing is produced for an API-key sentinel (that variable already reaches tool
 * subprocesses), when the base URL / socket is redirected, or when anything looks
 * like a real credential.
 */
export function claudeGatewayStub(config: ContainerConfig): ClaudeGatewayStub | null {
  const env = config.env ?? {};
  const sentinel = env[CLAUDE_OAUTH_VARIABLE];
  if (!isGatewaySentinel(sentinel)) return null;
  if (env.ANTHROPIC_BASE_URL || env.ANTHROPIC_UNIX_SOCKET) return null;
  const proxy = proxyAddress(env.HTTPS_PROXY ?? env.https_proxy);
  const ca = config.caCertificateContainerPath;
  if (!proxy || typeof ca !== "string" || !ca.startsWith("/")) return null;
  return {
    version: 1,
    provider: "anthropic",
    variable: CLAUDE_OAUTH_VARIABLE,
    sentinel,
    proxy,
    ca,
  };
}

/** Add the stub to the config's credential stubs (replacing any file at its path). */
export function withClaudeDelegateRuntime(config: ContainerConfig): ContainerConfig {
  const others = (config.credentialStubs ?? []).filter(
    (stub) => stub.containerPath !== CLAUDE_GATEWAY_STUB_PATH,
  );
  const stub = claudeGatewayStub(config);
  if (!stub) {
    return others.length === (config.credentialStubs ?? []).length
      ? config
      : { ...config, credentialStubs: others };
  }
  return {
    ...config,
    credentialStubs: [
      ...others,
      { containerPath: CLAUDE_GATEWAY_STUB_PATH, content: JSON.stringify(stub, null, 2) },
    ],
  };
}
