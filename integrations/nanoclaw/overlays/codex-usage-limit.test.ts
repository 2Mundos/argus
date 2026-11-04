import { describe, expect, it } from "bun:test";
import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { codexUsageLimitFailure } from "./codex-usage-limit.js";

function quotaNoticesFixturePath(): string {
  const installed = fileURLToPath(
    new URL("../../../../src/channels/argus-quota-notices.json", import.meta.url),
  );
  if (existsSync(installed)) return installed;
  const local = fileURLToPath(new URL("./quota-notices.json", import.meta.url));
  if (existsSync(local)) return local;
  return fileURLToPath(
    new URL("../../../packages/core/tests/fixtures/quota-notices.json", import.meta.url),
  );
}

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

  it("keeps zoneless ISO reset text exactly as the provider wrote it", () => {
    expect(
      codexUsageLimitFailure("You've hit your usage limit (resets 2026-11-19T14:08:00)")
        ?.reset_text,
    ).toBe("2026-11-19T14:08:00");
  });

  it("uses the earliest reset marker when both parenthetical and try-again appear", () => {
    const notice =
      "You've hit your usage limit (resets 2026-11-19T14:08:00). Upgrade, or try again at 3pm.";
    expect(codexUsageLimitFailure(notice)?.reset_text).toBe("2026-11-19T14:08:00");
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
    "I read somewhere that you've hit your usage limit",
    "The model said: you exceeded your current quota",
    "Error in tests: AssertionError: you've hit your usage limit",
    "Tests timed out after 30s",
  ])("is undefined for %p", (text) => {
    expect(codexUsageLimitFailure(text)).toBeUndefined();
  });
});

describe("codexUsageLimitFailure against shared quota notices fixture", () => {
  const allCases = JSON.parse(readFileSync(quotaNoticesFixturePath(), "utf8")) as Array<{
    provider: string;
    notice: string;
    exhausted: boolean;
    reset_status?: string;
  }>;
  const cases = allCases.filter((c) => c.provider === "openai");

  it.each(cases)("$notice", (c) => {
    const isAuthExpired = c.notice.includes("401 token_expired");
    const expected = c.exhausted && !isAuthExpired;
    const f = codexUsageLimitFailure(c.notice);
    if (!expected) {
      expect(f).toBeUndefined();
    } else {
      expect(f).toMatchObject({ provider: "codex", kind: "rate_limit" });
      if (c.reset_status === "ambiguous") {
        if (c.notice === "You've hit your usage limit (resets 2026-11-19T14:08:00)") {
          expect(f?.reset_text).toBe("2026-11-19T14:08:00");
        } else {
          expect(f?.reset_text).toBeTruthy();
        }
      }
    }
  });
});
