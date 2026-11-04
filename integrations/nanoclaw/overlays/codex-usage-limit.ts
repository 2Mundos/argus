/**
 * Codex's usage-limit block, recognised in the NanoClaw agent runner (installed into
 * container/agent-runner/src/providers/ by install.sh, hooked into poll-loop.ts by
 * patch-claude-rate-limit.py).
 *
 * The app-server answers a spent plan with a structured error (`codexErrorInfo`
 * `usageLimitExceeded` / `usage_limit_exceeded`) and the text "You've hit your usage
 * limit. Upgrade to Plus … or try again at <date>." (ASCII or typographic apostrophe).
 * Upstream only classifies `auth_expired`, so this turns the limit into the structured
 * providerFailure `{provider:"codex", kind:"rate_limit", notice?, reset_text?}`.
 * The reset is the provider's own words: no zone is guessed and no time is computed.
 */

const NOTICE_PREFIX =
  /^(?:(?:codex |claude )?error:\s*|turn failed:\s*|reconnecting\.{3}\s*\d+\/\d+:\s*)/i;

const STRUCTURED_QUOTA_CODES = new Set([
  "usagelimitexceeded",
  "usage_limit_exceeded",
  "usage_limit_reached",
  "insufficient_quota",
]);

const LIMIT_TEXT =
  /^(?:usage_limit_(?:reached|exceeded)|usagelimitexceeded|insufficient_quota|you(?:'|’)ve (?:hit|reached) your usage limit|you have (?:hit|reached) your usage limit|you exceeded your current quota)\b/i;
const RESET_WORDS = /\btry again at ([^\n]{3,60}?)\s*(?:\.(?:\s|$)|$)/i;
const NOTICE_MAX = 300;

export interface CodexRateLimitFailure {
  provider: "codex";
  kind: "rate_limit";
  /** The provider's sentence, one line, query strings and token-like strings dropped. */
  notice?: string;
  /** The reset as the provider wrote it; never normalised. */
  reset_text?: string;
}

function oneLine(text: string): string {
  return text
    .replace(/[\x00-\x1f\x7f]+/g, " ")
    .replace(/\b(?:bearer\s+|sk-|eyJ)[A-Za-z0-9._~+/=-]{8,}/gi, "[redacted]")
    .replace(/(https?:\/\/[^\s?#)]+)[?#][^\s)]*/g, "$1")
    .replace(/\s{2,}/g, " ")
    .trim();
}

function unwrapStructured(body: string): { text: string; isStructuredLimit: boolean } {
  if (!body.startsWith("{")) return { text: body, isStructuredLimit: false };
  let doc: unknown;
  try {
    doc = JSON.parse(body);
  } catch {
    return { text: body, isStructuredLimit: false };
  }
  if (!doc || typeof doc !== "object") return { text: body, isStructuredLimit: false };
  const fields = (
    "error" in doc && doc.error && typeof doc.error === "object" ? doc.error : doc
  ) as Record<string, unknown>;
  const message = typeof fields.message === "string" ? fields.message : undefined;
  for (const key of ["codexErrorInfo", "code", "type"]) {
    const value = fields[key];
    if (typeof value === "string" && STRUCTURED_QUOTA_CODES.has(value.toLowerCase())) {
      return {
        text: message !== undefined ? `${value}: ${message}` : value,
        isStructuredLimit: true,
      };
    }
  }
  return { text: message ?? body, isStructuredLimit: false };
}

/** The providerFailure for a failed Codex turn's error text, or undefined for anything else. */
export function codexUsageLimitFailure(
  error: string | undefined,
): CodexRateLimitFailure | undefined {
  if (!error) return undefined;
  const stripped = error.replace(NOTICE_PREFIX, "").trim();
  const { text: unwrapped, isStructuredLimit } = unwrapStructured(stripped);
  const candidate = unwrapped.replace(NOTICE_PREFIX, "").trim();
  if (!isStructuredLimit && !LIMIT_TEXT.test(candidate)) return undefined;

  const resetTarget = unwrapped || error;
  const reset = RESET_WORDS.exec(resetTarget.replace(/[\r\n]+/g, " ").trim());
  const notice = oneLine(unwrapped || error).slice(0, NOTICE_MAX);
  return {
    provider: "codex",
    kind: "rate_limit",
    ...(notice ? { notice } : {}),
    ...(reset?.[1] ? { reset_text: oneLine(reset[1]) } : {}),
  };
}
