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

const LIMIT_TEXT =
  /\byou(?:'|’)ve hit your usage limit\b|\byou have (?:hit|reached) your usage limit\b|\busage[_ ]?limit[_ ](?:reached|exceeded)\b|\binsufficient_quota\b|\busageLimitExceeded\b/i;
const RESET_WORDS = /\btry again at ([^.\n]{3,60}?)\s*\.?\s*$/im;
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

/** The providerFailure for a failed Codex turn's error text, or undefined for anything else. */
export function codexUsageLimitFailure(
  error: string | undefined,
): CodexRateLimitFailure | undefined {
  if (!error || !LIMIT_TEXT.test(error)) return undefined;
  const reset = RESET_WORDS.exec(error.replace(/[\r\n]+/g, " ").trim());
  const notice = oneLine(error).slice(0, NOTICE_MAX);
  return {
    provider: "codex",
    kind: "rate_limit",
    ...(notice ? { notice } : {}),
    ...(reset?.[1] ? { reset_text: oneLine(reset[1]) } : {}),
  };
}
