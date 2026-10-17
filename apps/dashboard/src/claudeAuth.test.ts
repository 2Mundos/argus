import { describe, expect, it } from "vitest";
import {
  checkClaudeCredential,
  checkToken,
  cleanCode,
  safeClaudeUrl,
  tokenCredential,
} from "./claudeAuth";

const NOW = Date.UTC(2026, 0, 1);
// Built at runtime so no literal looks like a real credential.
const OAT = `sk-ant-${"oat01-"}${"x".repeat(40)}`;
const API = `sk-ant-${"api03-"}${"y".repeat(40)}`;
const login = (over: Record<string, unknown> = {}) =>
  JSON.stringify({
    claudeAiOauth: {
      accessToken: "a".repeat(20),
      refreshToken: "r".repeat(20),
      expiresAt: Date.UTC(2026, 5, 1),
      ...over,
    },
  });

describe("checkClaudeCredential", () => {
  it("derives the kind from the document", () => {
    expect(checkClaudeCredential(JSON.stringify({ api_key: API }), NOW)).toEqual({
      ok: true,
      mode: "api_key",
      kind: "api_key",
    });
    expect(checkClaudeCredential(JSON.stringify({ oauth_token: OAT }), NOW)).toEqual({
      ok: true,
      mode: "oauth_token",
      kind: "subscription",
    });
    expect(checkClaudeCredential(login(), NOW)).toEqual({
      ok: true,
      mode: "claude_ai_oauth",
      kind: "subscription",
    });
  });

  it("names the problem and never repeats a value", () => {
    const cases: Array<[string, RegExp]> = [
      ["nope", /not valid JSON/],
      ["[]", /JSON object/],
      [JSON.stringify({ token: OAT }), /Needs one of/],
      [JSON.stringify({ api_key: API, oauth_token: OAT }), /only one of/],
      [JSON.stringify({ api_key: OAT }), /use oauth_token/],
      [JSON.stringify({ oauth_token: API }), /use api_key/],
      [JSON.stringify({ api_key: "has space" }), /whitespace/],
      [JSON.stringify({ claudeAiOauth: { accessToken: OAT } }), /missing: refreshToken/],
      [login({ expiresAt: 1000 }), /in the past/],
      [login({ expiresAt: "soon" }), /not a date/],
    ];
    for (const [text, expected] of cases) {
      const r = checkClaudeCredential(text, NOW);
      expect(r.ok).toBe(false);
      if (r.ok) continue;
      expect(r.error).toMatch(expected);
      for (const secret of [OAT, API, "has space", "a".repeat(20)]) {
        expect(r.error).not.toContain(secret);
      }
    }
  });
});

describe("tokens", () => {
  it("builds the document of the chosen type and keeps the token intact", () => {
    expect(tokenCredential("oauth_token", `  ${OAT}\n`)).toEqual({ oauth_token: OAT });
    expect(tokenCredential("api_key", API)).toEqual({ api_key: API });
  });

  it("refuses a token of the wrong type", () => {
    expect(checkToken("api_key", OAT).ok).toBe(false);
    expect(checkToken("oauth_token", API).ok).toBe(false);
    expect(checkToken("oauth_token", OAT).ok).toBe(true);
    expect(checkToken("api_key", "  ").ok).toBe(false);
  });
});

describe("cleanCode", () => {
  it("trims paste whitespace only and refuses anything else odd", () => {
    expect(cleanCode("  abc-DEF_1.2~3#st%2B+/=  \n")).toBe("abc-DEF_1.2~3#st%2B+/=");
    for (const bad of ["", "   ", "a b", "é", "$(id)", "a;b", "x".repeat(2049)]) {
      expect(cleanCode(bad)).toBeNull();
    }
  });
});

describe("safeClaudeUrl", () => {
  it("links only https Claude/Anthropic hosts", () => {
    expect(safeClaudeUrl("https://claude.ai/oauth/authorize?a=1")).toContain("claude.ai");
    expect(safeClaudeUrl("https://platform.claude.com/x")).not.toBeNull();
    for (const bad of [
      null,
      "http://claude.ai/x",
      "https://claude.ai.evil.example/x",
      "https://evilclaude.ai/x",
      "https://u:p@claude.ai/x",
      "https://claude.ai@evil.example/x",
      "javascript:alert(1)",
    ]) {
      expect(safeClaudeUrl(bad)).toBeNull();
    }
  });
});
