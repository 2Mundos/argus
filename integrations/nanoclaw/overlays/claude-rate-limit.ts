/**
 * Claude Code's quota block, recognised in the NanoClaw agent runner (installed
 * into container/agent-runner/src/providers/ by integrations/nanoclaw/install.sh,
 * hooked into claude.ts and poll-loop.ts by patch-claude-rate-limit.py).
 *
 * A failed turn whose result is exactly one `You've hit your session|usage|weekly
 * limit · resets …` line, or that the SDK marked `rate_limit`, carries only that
 * line (or a fixed text) as its error; the poll loop turns it into the structured providerFailure
 * `{provider:"claude", kind:"rate_limit", resets_at?}` and keeps the generic
 * channel text, so no other upstream detail leaves the container.
 */

/** The whole result, one line: `.` stops at a newline and `$` is the end of the text. */
export const CLAUDE_LIMIT_LINE =
  /^You(?:'|\u2019)ve hit your (session|usage|weekly) limit · resets .+$/;

/** The error of a turn the SDK marked `rate_limit` whose text is not the line above. */
export const CLAUDE_RATE_LIMITED = "Rate limit reached";

export interface ClaudeRateLimitFailure {
  provider: "claude";
  kind: "rate_limit";
  /** ISO time the limit resets, when the line states it in UTC. */
  resets_at?: string;
}

/**
 * The `error` of a failed Claude turn that hit its quota, or undefined. `result`
 * is the SDK result text (Claude Code's notice, with an empty errors[]);
 * `sdkRateLimited` is whether the turn's assistant message had `error: 'rate_limit'`.
 */
export function claudeRateLimitError(
  result: string | undefined,
  sdkRateLimited: boolean,
): string | undefined {
  const line = result?.trim() ?? "";
  if (CLAUDE_LIMIT_LINE.test(line)) return line;
  return sdkRateLimited ? CLAUDE_RATE_LIMITED : undefined;
}

/** SDK assistant.error when the stored credential was rejected (claude-accounts.spec.md). */
export const CLAUDE_AUTH_FAILED = "authentication_failed";

/** The `error` of a failed turn the SDK marked authentication_failed, or undefined. */
export function claudeAuthExpiredError(sdkAuthFailed: boolean): string | undefined {
  return sdkAuthFailed ? CLAUDE_AUTH_FAILED : undefined;
}

export interface ClaudeAuthExpiredFailure {
  provider: "claude";
  kind: "auth_expired";
}

/** The providerFailure for `claudeAuthExpiredError`; undefined for anything else. */
export function claudeAuthExpiredFailure(
  error: string | undefined,
): ClaudeAuthExpiredFailure | undefined {
  if (error === CLAUDE_AUTH_FAILED) return { provider: "claude", kind: "auth_expired" };
  return undefined;
}

/** The providerFailure for an error `claudeRateLimitError` produced; undefined for anything else. */
export function claudeRateLimitFailure(
  error: string | undefined,
  now: Date = new Date(),
): ClaudeRateLimitFailure | undefined {
  if (error === CLAUDE_RATE_LIMITED) return { provider: "claude", kind: "rate_limit" };
  if (!error || !CLAUDE_LIMIT_LINE.test(error)) return undefined;
  const resets_at = utcClockReset(error, now);
  return { provider: "claude", kind: "rate_limit", ...(resets_at ? { resets_at } : {}) };
}

/** `resets 2am (UTC)` / `resets 2:30pm (UTC)`: the next such UTC time strictly after `now`. */
function utcClockReset(line: string, now: Date): string | undefined {
  const clock = /· resets (\d{1,2})(?::(\d{2}))?\s*([ap])m \(UTC\)$/i.exec(line);
  if (!clock) return undefined;
  const hour12 = Number(clock[1]);
  const minute = Number(clock[2] ?? "0");
  if (hour12 < 1 || hour12 > 12 || minute > 59) return undefined;
  const hour = (hour12 % 12) + (clock[3]!.toLowerCase() === "p" ? 12 : 0);
  const at = new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), hour, minute),
  );
  if (at.getTime() <= now.getTime()) at.setUTCDate(at.getUTCDate() + 1);
  return at.toISOString();
}
