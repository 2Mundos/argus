/** Client-side look at a Codex auth.json: its shape only, never a value. */
export type AuthFileCheck =
  | { ok: true; mode: "chatgpt" | "apikey"; kind: "subscription" | "api_key" }
  | { ok: false; error: string };

const TOKENS = ["id_token", "access_token", "refresh_token"] as const;
const filled = (v: unknown): boolean => typeof v === "string" && v.trim() !== "";

export function checkAuthFile(text: string): AuthFileCheck {
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    return { ok: false, error: "This is not valid JSON." };
  }
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) {
    return { ok: false, error: "An auth.json is a JSON object." };
  }
  const d = doc as Record<string, unknown>;
  if (d.auth_mode === "apikey") {
    return filled(d.OPENAI_API_KEY)
      ? { ok: true, mode: "apikey", kind: "api_key" }
      : { ok: false, error: "The API-key file has no OPENAI_API_KEY." };
  }
  if (d.auth_mode !== "chatgpt") {
    return { ok: false, error: "auth_mode must be chatgpt or apikey." };
  }
  const tokens = d.tokens;
  if (!tokens || typeof tokens !== "object") {
    return { ok: false, error: "The ChatGPT login has no tokens." };
  }
  const missing = TOKENS.filter((k) => !filled((tokens as Record<string, unknown>)[k]));
  return missing.length === 0
    ? { ok: true, mode: "chatgpt", kind: "subscription" }
    : { ok: false, error: `The tokens object is missing: ${missing.join(", ")}.` };
}

/** Only https links to OpenAI are shown as links (the API enforces it too). */
export function safeLoginUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const u = new URL(url);
    const ok = ["auth.openai.com", "chatgpt.com", "openai.com"].some(
      (h) => u.hostname === h || u.hostname.endsWith(`.${h}`),
    );
    return u.protocol === "https:" && !u.username && !u.password && ok ? u.toString() : null;
  } catch {
    return null;
  }
}
