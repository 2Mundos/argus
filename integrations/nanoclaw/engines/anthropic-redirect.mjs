/** Shared Anthropic API redirect rules (local harness spec): one exact allowed
 * base URL for a local lease; any other redirect is rejected, and a redirect
 * with a cloud credential is always rejected. */

export const LOCAL_HARNESS_AUTH_TOKEN = "argus-local-harness";
export const CUSTOM_HARNESS_AUTH_TOKEN = "argus-custom-harness";

export const LOCAL_ANTHROPIC_HOSTS = new Set([
  "127.0.0.1",
  "localhost",
  "::1",
  "host.docker.internal",
]);

/** True when `hostname` is a loopback or Docker-host alias (local harness spec). */
export function isLocalAnthropicHost(hostname) {
  return LOCAL_ANTHROPIC_HOSTS.has(String(hostname ?? "").toLowerCase());
}

/** Normalise like Argus `validate_local_base_url`: scheme, host, port; no path. */
export function normalizeAnthropicBaseUrl(raw) {
  if (typeof raw !== "string" || !raw.trim()) return null;
  try {
    const u = new URL(raw.trim());
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    if (u.username || u.password || u.search || u.hash) return null;
    if (u.pathname && u.pathname !== "/") return null;
    return `${u.protocol}//${u.host}`;
  } catch {
    return null;
  }
}

/** Cloud harness material that must never ride with a redirected API base URL. */
export function cloudHarnessCredentialPresent(e) {
  if ((e.ANTHROPIC_API_KEY ?? "").trim() || (e.CLAUDE_CODE_OAUTH_TOKEN ?? "").trim()) return true;
  const token = (e.ANTHROPIC_AUTH_TOKEN ?? "").trim();
  return token !== "" && token !== LOCAL_HARNESS_AUTH_TOKEN && token !== CUSTOM_HARNESS_AUTH_TOKEN;
}

/**
 * When `ANTHROPIC_BASE_URL` is set: accept only if it equals `allowedBaseUrl`
 * (from `ARGUS_ALLOWED_ANTHROPIC_BASE_URL`) and no cloud credential is present.
 * Returns a rejection reason code or null when the redirect is allowed.
 */
export function anthropicRedirectRejection(e, allowedBaseUrl) {
  if ((e.ANTHROPIC_UNIX_SOCKET ?? "").trim()) return "api_redirected";
  const base = (e.ANTHROPIC_BASE_URL ?? "").trim();
  if (!base) return null;
  const normBase = normalizeAnthropicBaseUrl(base);
  const normAllowed = normalizeAnthropicBaseUrl(allowedBaseUrl ?? "");
  if (!normBase || !normAllowed || normBase !== normAllowed) return "api_redirected";
  try {
    if (!isLocalAnthropicHost(new URL(normAllowed).hostname)) return "api_redirected";
  } catch {
    return "api_redirected";
  }
  if (cloudHarnessCredentialPresent(e)) return "api_redirected";
  return null;
}
