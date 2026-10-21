import { describe, expect, it } from "bun:test";

import { codexUsageLimitFailure } from "./codex-usage-limit.js";

const TEXT =
  "You’ve hit your usage limit. Upgrade to Plus to continue using Codex (https://chatgpt.com/explore/plus), or try again at Nov 19th, 2026 2:08 PM.";

describe("codexUsageLimitFailure", () => {
  it.each([
    ["typographic", TEXT],
    ["ASCII", TEXT.replace("’", "'")],
    ["structured", JSON.stringify({ message: TEXT, codexErrorInfo: "usageLimitExceeded" })],
  ])("recognises the %s notice and keeps the reset as the provider's words", (_n, text) => {
    const f = codexUsageLimitFailure(text);
    expect(f).toMatchObject({ provider: "codex", kind: "rate_limit" });
    expect(f).not.toHaveProperty("resets_at");
  });

  it("keeps the reset text unnormalised", () => {
    expect(codexUsageLimitFailure(TEXT)?.reset_text).toBe("Nov 19th, 2026 2:08 PM");
  });

  it("takes the reset from its sentence, not only at the end of the message", () => {
    const f = codexUsageLimitFailure(
      "You've hit your usage limit, try again at 3pm. Upgrade to Plus to continue.",
    );
    expect(f?.reset_text).toBe("3pm");
    expect(
      codexUsageLimitFailure("You've hit your usage limit. try again at 3 p.m. Upgrade now.")
        ?.reset_text,
    ).toBe("3 p.m");
  });

  it("leaves reset_text out when the provider stated none", () => {
    const f = codexUsageLimitFailure("usage_limit_reached");
    expect(f).toEqual({ provider: "codex", kind: "rate_limit", notice: "usage_limit_reached" });
  });

  it("drops query strings and token-like strings from the notice", () => {
    const f = codexUsageLimitFailure(`${TEXT} https://x.io/a?token=zzz Bearer abcdefghijkl`);
    expect(f?.notice).not.toMatch(/zzz|abcdefghijkl/);
  });

  it.each([
    undefined,
    "",
    "401 unauthorized: token_expired",
    "fetch failed: ETIMEDOUT",
    "AssertionError: expected 3",
    "I read that you've hit your limit somewhere",
  ])("is undefined for %p", (text) => {
    expect(codexUsageLimitFailure(text)).toBeUndefined();
  });
});
