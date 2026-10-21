import { describe, expect, it } from "bun:test";

import {
  CLAUDE_RATE_LIMITED,
  claudeRateLimitError,
  claudeRateLimitFailure,
} from "./claude-rate-limit.js";

const LINE = "You've hit your session limit · resets 2am (UTC)";
const NOW = new Date("2026-10-20T22:06:00Z");

describe("claudeRateLimitError", () => {
  it.each([
    LINE,
    "You\u2019ve hit your usage limit · resets 2:30pm (UTC)",
    "You've hit your weekly limit · resets Oct 27, 9am (Europe/Lisbon)",
  ])("keeps Claude Code's exact one-line notice (%s)", (line) => {
    expect(claudeRateLimitError(`  ${line}\n`, false)).toBe(line);
  });

  it("uses the fixed error when the SDK marked the turn rate_limit with other text", () => {
    expect(claudeRateLimitError('API Error: 429 {"error":"tenant private"}', true)).toBe(
      CLAUDE_RATE_LIMITED,
    );
    expect(claudeRateLimitError(undefined, true)).toBe(CLAUDE_RATE_LIMITED);
  });

  it.each([
    `I think ${LINE}`,
    `${LINE}\nsecond line`,
    "You've hit your daily limit · resets 2am (UTC)",
    "Rate limit reached for tenant private-customer",
    "",
  ])("ignores anything else (%p)", (text) => {
    expect(claudeRateLimitError(text, false)).toBeUndefined();
  });
});

describe("claudeRateLimitFailure", () => {
  it("parses the UTC reset as the next such time", () => {
    expect(claudeRateLimitFailure(LINE, NOW)).toEqual({
      provider: "claude",
      kind: "rate_limit",
      resets_at: "2026-10-21T02:00:00.000Z",
    });
    expect(
      claudeRateLimitFailure("You've hit your usage limit · resets 11:30pm (UTC)", NOW),
    ).toEqual({
      provider: "claude",
      kind: "rate_limit",
      resets_at: "2026-10-20T23:30:00.000Z",
    });
  });

  it("omits resets_at when the reset is not a UTC clock time", () => {
    for (const error of [
      "You've hit your session limit · resets 2am (America/Sao_Paulo)",
      "You've hit your session limit · resets 13am (UTC)",
      CLAUDE_RATE_LIMITED,
    ]) {
      expect(claudeRateLimitFailure(error, NOW)).toEqual({
        provider: "claude",
        kind: "rate_limit",
      });
    }
  });

  it.each([
    undefined,
    "",
    `Error: ${LINE}`,
    `${LINE}\nmore`,
    "billing hard-stop",
    "Rate limit reached (x)",
  ])("is undefined for anything else (%p)", (error) => {
    expect(claudeRateLimitFailure(error, NOW)).toBeUndefined();
  });
});
