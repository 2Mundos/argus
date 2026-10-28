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
  return undefined;
}

export function localBrokerNotice(): string {
  return (
    "The local model is reached through Argus (run_local / broker), not an API " +
    "provider account. Do not create a fake local account here."
  );
}
