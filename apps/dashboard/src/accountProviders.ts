import type { EngineCatalog } from "./api";

/** UI-only: never sent to the API. */
export const PROVIDER_PICK_CUSTOM = "__custom__";

type AccountProviderOption = {
  id: string;
  label: string;
  authMethods: string[];
};

/** Not engines: service credentials workers use for repository access. */
export const GITEA_PROVIDER = "gitea";
export const GITHUB_PROVIDER = "github";
/** Executor-only subscription account: a token the operator already owns. */
export const COPILOT_PROVIDER = "copilot";

const PROVIDER_ID = /^[a-z0-9_-]{1,50}$/;

/** HTML `pattern` for custom provider id (hyphen escaped for Unicode Sets / Chrome). */
export const CUSTOM_PROVIDER_PATTERN = "[a-z0-9_\\-]{1,50}";

export function providerOptionsFromCatalog(catalog: EngineCatalog): AccountProviderOption[] {
  const seen = new Set<string>();
  const out: AccountProviderOption[] = [];
  for (const e of catalog.engines) {
    const id = e.axes.account_provider;
    if (!id || seen.has(id)) continue;
    seen.add(id);
    const auth = e.auth_methods.length > 0 ? e.auth_methods.join(", ") : "see engine notes";
    out.push({
      id,
      label: `${id} (${e.axes.model_provider}) — ${auth}`,
      authMethods: e.auth_methods,
    });
  }
  if (!seen.has(GITEA_PROVIDER)) {
    out.push({
      id: GITEA_PROVIDER,
      label: "gitea (repository access) — username + personal access token",
      authMethods: ["personal-access-token"],
    });
  }
  if (!seen.has(GITHUB_PROVIDER)) {
    out.push({
      id: GITHUB_PROVIDER,
      label: "github (repository access) — fine-grained personal access token",
      authMethods: ["personal-access-token"],
    });
  }
  if (!seen.has(COPILOT_PROVIDER)) {
    out.push({
      id: COPILOT_PROVIDER,
      label: "copilot (delegated CLI) — operator-owned token",
      authMethods: ["token"],
    });
  }
  return out.sort((a, b) => a.id.localeCompare(b.id));
}

export function validateProviderId(id: string): string | null {
  if (!id || id === PROVIDER_PICK_CUSTOM) return "choose a provider id";
  if (!PROVIDER_ID.test(id)) {
    return "Provider must match [a-z0-9_-]+ and be 1–50 characters.";
  }
  return null;
}

/** Argus JSON account ≠ native CLI login (Codex device-auth, etc.). */
export function nativeAuthNotice(providerId: string): string | undefined {
  if (providerId === "openai") {
    return (
      "A Codex account is an auth.json (ChatGPT login or API key). Use “Add Codex account” " +
      "to import one or generate it with a dedicated login; workers get it through the vault."
    );
  }
  if (providerId === "anthropic") {
    return (
      "This JSON account is for Argus task leasing. Use “Add Claude account” to import a " +
      "credential or sign in with a dedicated login, and “Renew credential” on an existing " +
      "account. A Claude Code login inside a worker is separate."
    );
  }
  if (providerId === GITEA_PROVIDER) {
    return (
      "A Gitea account is a username and personal access token for repository access. " +
      "It is leased only to the repository role and injected by the proxy, never given to the model."
    );
  }
  if (providerId === GITHUB_PROVIDER) {
    return (
      "A GitHub account is a fine-grained personal access token limited to the allowlisted " +
      "repositories (contents, pull requests and issues read/write; commit statuses, checks " +
      "and actions read). It is used for github.com repositories, leased only to the " +
      "repository role and injected by the proxy, never given to the model."
    );
  }
  if (providerId === COPILOT_PROVIDER) return copilotTokenNotice();
  return undefined;
}

const COPILOT_PREFIXES = ["github_pat_", "gho_", "ghu_"];

/** Accepted and rejected Copilot token types, as the import form states them. */
export function copilotTokenNotice(): string {
  return (
    'A Copilot account is a token you already own, stored as {"token": …} with kind ' +
    "subscription. Accepted: a fine-grained personal access token (github_pat_, with the " +
    "Copilot Requests permission), an OAuth token from copilot login (gho_), or a GitHub App " +
    "user-to-server token (ghu_). A classic personal access token (ghp_) is rejected: the " +
    "Copilot CLI ignores it. It is used only as an executor, never as an engine, and is " +
    "write-only: it is never shown again."
  );
}

/**
 * UX hint only; the API is authoritative. Names the rule, never the value.
 * Returns an error message, or null when the prefix looks acceptable.
 */
export function validateCopilotToken(token: string): string | null {
  const value = token.trim();
  if (!value) return "Enter a token.";
  if (value.startsWith("ghp_")) {
    return "Classic personal access tokens (ghp_) are not accepted. Use a fine-grained token (github_pat_), an OAuth token (gho_) or a GitHub App user-to-server token (ghu_).";
  }
  if (!COPILOT_PREFIXES.some((p) => value.startsWith(p))) {
    return "The token must start with github_pat_, gho_ or ghu_.";
  }
  return null;
}

/** Drops a submitted secret from a message before it is shown. */
export function withoutSecret(message: string, secret: string): string {
  const value = secret.trim();
  return value ? message.split(value).join("[redacted]") : message;
}

export function localBrokerNotice(): string {
  return (
    "The local model is reached through Argus (run_local / broker), not an API " +
    "provider account. Do not create a fake local account here."
  );
}
