/**
 * Make the Codex sentinel auth.json name the real ChatGPT workspace.
 *
 * OneCLI serves the container a stub `~/.codex/auth.json` whose account id is
 * the constant "onecli-managed" and swaps the real token on the wire. Codex
 * checks its own account id against the workspaces the backend lists for the
 * real token ("selected workspace missing from routing discovery"), so every
 * model call fails. The workspace id is an identifier, not a credential; the
 * vault keeps it as secret metadata. This copies it into the stub (also into the
 * stub id_token's claim) and leaves everything else, tokens included, untouched.
 */
import type { ContainerConfig } from "@onecli-sh/sdk";

const AUTH_CLAIM = "https://api.openai.com/auth";

function patchIdToken(token: string, accountId: string): string {
  const parts = token.split(".");
  if (parts.length !== 3) return token;
  try {
    const claims = JSON.parse(Buffer.from(parts[1]!, "base64url").toString("utf8"));
    if (!claims || typeof claims !== "object" || Array.isArray(claims)) return token;
    const auth = claims[AUTH_CLAIM];
    if (auth && typeof auth === "object") auth.chatgpt_account_id = accountId;
    parts[1] = Buffer.from(JSON.stringify(claims)).toString("base64url");
    return parts.join(".");
  } catch {
    return token;
  }
}

/** The stub with the real workspace id, or the input unchanged when it is not a ChatGPT stub. */
export function mirrorAccountId(content: string, accountId: string): string {
  try {
    const doc = JSON.parse(content);
    if (doc?.auth_mode !== "chatgpt" || !doc.tokens || typeof doc.tokens !== "object") {
      return content;
    }
    doc.tokens.account_id = accountId;
    if (typeof doc.tokens.id_token === "string") {
      doc.tokens.id_token = patchIdToken(doc.tokens.id_token, accountId);
    }
    return JSON.stringify(doc, null, 2);
  } catch {
    return content;
  }
}

const isCodexStub = (stub: { containerPath: string }) =>
  stub.containerPath.endsWith("/.codex/auth.json");

/**
 * Patch the Codex stub of `config` with the workspace id from `lookup`. The
 * lookup runs only when the config carries a Codex stub, so other agents pay
 * nothing; a missing id leaves the config as it was.
 */
export async function withMirroredCodexAccount(
  config: ContainerConfig,
  lookup: () => Promise<string | null>,
): Promise<ContainerConfig> {
  if (!config.credentialStubs?.some(isCodexStub)) return config;
  const accountId = await lookup();
  if (!accountId) return config;
  return {
    ...config,
    credentialStubs: config.credentialStubs.map((stub) =>
      isCodexStub(stub) ? { ...stub, content: mirrorAccountId(stub.content, accountId) } : stub,
    ),
  };
}

/** The workspace id OneCLI recorded for this agent group's Argus-managed OpenAI secret. */
export async function codexAccountId(
  onecliUrl: string,
  apiKey: string | undefined,
  agentGroupId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<string | null> {
  try {
    const res = await fetchImpl(`${onecliUrl.replace(/\/$/, "")}/api/secrets`, {
      headers: apiKey ? { Authorization: `Bearer ${apiKey}` } : {},
    });
    if (!res.ok) return null;
    const secrets = (await res.json()) as Array<{
      name?: string;
      metadata?: { accountId?: unknown };
    }>;
    const id = secrets.find((s) => s.name === `argus-${agentGroupId}-openai`)?.metadata?.accountId;
    return typeof id === "string" && id ? id : null;
  } catch {
    return null;
  }
}
