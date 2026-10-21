/** Shared lease `settings` for custom accounts (custom accounts spec). No secrets. */

export const CUSTOM_KEY_ENV = "ARGUS_ACCOUNT_API_KEY";
export const CUSTOM_PROVIDER_ID = "argus_custom";
const MODEL_ID = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$/;
const isObject = (x) => x !== null && typeof x === "object" && !Array.isArray(x);

/** Validated settings, null when absent; throws on a malformed document (value never echoed). */
export function parseAccountSettings(raw) {
  if (raw === undefined || raw === null || raw === "") return null;
  let doc = raw;
  if (typeof raw === "string") {
    try {
      doc = JSON.parse(raw);
    } catch {
      throw new Error("account settings are not valid JSON");
    }
  }
  if (!isObject(doc)) throw new Error("account settings must be an object");
  let url;
  try {
    url = new URL(doc.base_url);
  } catch {
    throw new Error("account settings base_url is not a URL");
  }
  if (
    (url.protocol !== "http:" && url.protocol !== "https:") ||
    !url.hostname ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  )
    throw new Error(
      "account settings base_url must be http(s) without credentials, query or fragment",
    );
  if (doc.model != null && !(typeof doc.model === "string" && MODEL_ID.test(doc.model)))
    throw new Error("account settings model is invalid");
  if (doc.provider != null && !isObject(doc.provider))
    throw new Error("account settings provider must be an object");
  return {
    base_url: String(doc.base_url).replace(/\/+$/, ""),
    label: typeof doc.label === "string" ? doc.label : null,
    model: doc.model ?? null,
    provider: doc.provider ?? null,
  };
}

/** What the executor can take from the settings; Codex cannot add body fields (`provider`). */
export function settingsSupport(executor) {
  return (
    { opencode: ["base_url", "model", "provider"], codex: ["base_url", "model"] }[executor] ?? []
  );
}
