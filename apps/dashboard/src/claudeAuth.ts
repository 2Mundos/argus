/** Client-side look at an Anthropic credential: its shape only, never a value. */
export type ClaudeCheck =
  | {
      ok: true;
      mode: "api_key" | "oauth_token" | "claude_ai_oauth";
      kind: "subscription" | "api_key";
    }
  | { ok: false; error: string };

export type TokenType = "oauth_token" | "api_key";

const OAUTH_PREFIX = "sk-ant-oat";
const API_PREFIX = "sk-ant-api";
const SIGN_IN_CODE = /^[A-Za-z0-9_-]{20,}#[A-Za-z0-9_-]{20,}$/;
const MAX_TOKEN = 4096;
const KEYS = ["api_key", "oauth_token", "claudeAiOauth"] as const;
const filled = (v: unknown): v is string => typeof v === "string" && v.trim() !== "";

function tokenError(value: unknown, label: string): string | null {
  if (!filled(value)) return `${label} must be a non-empty string.`;
  if (value.length > MAX_TOKEN) return `${label} is too long.`;
  if (/\s/.test(value)) return `${label} must not contain whitespace.`;
  return null;
}

function expiryMs(v: unknown): number | null | "bad" {
  if (v === undefined || v === null) return null;
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.trim()) {
    const t = Date.parse(v);
    return Number.isFinite(t) ? t : "bad";
  }
  return "bad";
}

export function checkClaudeCredential(text: string, now = Date.now()): ClaudeCheck {
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    return { ok: false, error: "This is not valid JSON." };
  }
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) {
    return { ok: false, error: "A credential is a JSON object." };
  }
  const d = doc as Record<string, unknown>;
  const present = KEYS.filter((k) => k in d);
  if (present.length === 0) {
    return { ok: false, error: "Needs one of: api_key, oauth_token, claudeAiOauth." };
  }
  if (present.length > 1) return { ok: false, error: `Give only one of: ${present.join(", ")}.` };
  if (present[0] === "api_key") {
    const bad = tokenError(d.api_key, "api_key");
    if (bad) return { ok: false, error: bad };
    const key = String(d.api_key);
    if (key.startsWith(OAUTH_PREFIX)) {
      return { ok: false, error: "api_key holds an OAuth token; use oauth_token." };
    }
    return key.startsWith(API_PREFIX)
      ? { ok: true, mode: "api_key", kind: "api_key" }
      : { ok: false, error: `api_key is not an Anthropic API key; it starts with ${API_PREFIX}.` };
  }
  if (present[0] === "oauth_token") {
    const bad = tokenError(d.oauth_token, "oauth_token");
    if (bad) return { ok: false, error: bad };
    const token = String(d.oauth_token);
    if (token.startsWith(API_PREFIX)) {
      return { ok: false, error: "oauth_token holds an API key; use api_key." };
    }
    if (token.startsWith(OAUTH_PREFIX))
      return { ok: true, mode: "oauth_token", kind: "subscription" };
    return {
      ok: false,
      error: SIGN_IN_CODE.test(token)
        ? `oauth_token holds the code the sign-in page shows, not a token: that code goes in the Sign in with Claude step. The token comes from claude setup-token and starts with ${OAUTH_PREFIX}.`
        : `oauth_token is not a token: it must hold the token claude setup-token prints, which starts with ${OAUTH_PREFIX}.`,
    };
  }
  const o = d.claudeAiOauth;
  if (!o || typeof o !== "object" || Array.isArray(o)) {
    return { ok: false, error: "claudeAiOauth must be an object." };
  }
  const oauth = o as Record<string, unknown>;
  const missing = ["accessToken", "refreshToken"].filter((k) => !filled(oauth[k]));
  if (missing.length > 0) {
    return { ok: false, error: `claudeAiOauth is missing: ${missing.join(", ")}.` };
  }
  for (const k of ["accessToken", "refreshToken"]) {
    const bad = tokenError(oauth[k], `claudeAiOauth.${k}`);
    if (bad) return { ok: false, error: bad };
  }
  const expires = expiryMs(oauth.expiresAt);
  if (expires === "bad") return { ok: false, error: "claudeAiOauth.expiresAt is not a date." };
  if (expires !== null && expires <= now) {
    return {
      ok: false,
      error: "claudeAiOauth.expiresAt is in the past; sign in again for a fresh credential.",
    };
  }
  return { ok: true, mode: "claude_ai_oauth", kind: "subscription" };
}

/** A token the operator already has, with its type chosen explicitly. Surrounding
 * whitespace from a paste is dropped; the token itself is never altered. */
export function tokenCredential(type: TokenType, token: string): Record<string, string> {
  return { [type]: token.trim() };
}

export function checkToken(type: TokenType, token: string): ClaudeCheck {
  return checkClaudeCredential(JSON.stringify(tokenCredential(type, token)));
}

const CODE = /^[A-Za-z0-9._~#%+/=-]{1,2048}$/;

/** The code the sign-in page shows: pasted whitespace is trimmed, nothing else. */
export function cleanCode(raw: string): string | null {
  const code = raw.trim();
  return CODE.test(code) ? code : null;
}

/** Only https links to Claude/Anthropic are shown as links (the API enforces it too). */
export function safeClaudeUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    const ok = ["claude.ai", "claude.com", "anthropic.com"].some(
      (h) => u.hostname === h || u.hostname.endsWith(`.${h}`),
    );
    return u.protocol === "https:" && !u.username && !u.password && ok ? u.toString() : null;
  } catch {
    return null;
  }
}
