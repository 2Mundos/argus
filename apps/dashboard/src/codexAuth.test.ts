import { describe, expect, it } from "vitest";
import { checkAuthFile, safeLoginUrl } from "./codexAuth";

const login = (over: Record<string, unknown> = {}) =>
  JSON.stringify({
    auth_mode: "chatgpt",
    tokens: { id_token: "i", access_token: "a", refresh_token: "r", account_id: "x" },
    ...over,
  });

describe("checkAuthFile", () => {
  it("recognises a ChatGPT login and an API-key file", () => {
    expect(checkAuthFile(login())).toEqual({ ok: true, mode: "chatgpt", kind: "subscription" });
    expect(checkAuthFile('{"auth_mode":"apikey","OPENAI_API_KEY":"sk"}')).toEqual({
      ok: true,
      mode: "apikey",
      kind: "api_key",
    });
  });

  it("names what is wrong without echoing values", () => {
    for (const text of [
      "nope",
      "[]",
      "null",
      '{"auth_mode":"other"}',
      '{"auth_mode":"apikey","OPENAI_API_KEY":""}',
      '{"auth_mode":"chatgpt"}',
      '{"auth_mode":"chatgpt","tokens":{"access_token":"LEAK"}}',
    ]) {
      const r = checkAuthFile(text);
      expect(r.ok).toBe(false);
      if (!r.ok) expect(r.error).not.toContain("LEAK");
    }
    const r = checkAuthFile('{"auth_mode":"chatgpt","tokens":{"access_token":"LEAK"}}');
    expect(r).toEqual({
      ok: false,
      error: "The tokens object is missing: id_token, refresh_token.",
    });
  });
});

describe("safeLoginUrl", () => {
  it("allows https OpenAI hosts only", () => {
    expect(safeLoginUrl("https://auth.openai.com/codex/device")).toBe(
      "https://auth.openai.com/codex/device",
    );
    for (const bad of [
      "http://auth.openai.com/x",
      "https://auth.openai.com.evil.example/",
      "https://u:p@auth.openai.com/",
      "javascript:alert(1)",
      "",
      null,
      undefined,
    ]) {
      expect(safeLoginUrl(bad)).toBeNull();
    }
  });
});
