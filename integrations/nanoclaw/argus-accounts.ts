/**
 * Account switching for the Argus channel.
 *
 * Argus owns the pool of provider accounts and leases one to this agent. The
 * credential never goes to the agent container: it is written into the OneCLI
 * vault, which injects it into the agent's API calls at the network boundary.
 * Replacing the secret's value switches the account for a running agent with no
 * restart (verified against OneCLI 1.42; docs/research/…nanoclaw-integration).
 *
 * One OneCLI secret per agent group (`argus-<agent group id>`), assigned to that
 * group's OneCLI agent in selective mode so it is the only secret the agent can
 * use; a switch is a `PATCH` of its value.
 */

export interface AccountLease {
  lease_id: string;
  account_id: string;
  name: string;
  provider: string;
  kind: string;
  credential: Record<string, unknown>;
}

/** How a provider's account becomes a OneCLI secret. */
interface ProviderMapping {
  type: string;
  host: string;
  /** The secret value out of an Argus credential document. */
  pick(credential: Record<string, unknown>): string | null;
}

const text = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v.trim() : null);

export const PROVIDERS: Record<string, ProviderMapping> = {
  // An API key, or a subscription's OAuth token; OneCLI tells them apart.
  anthropic: {
    type: "anthropic",
    host: "api.anthropic.com",
    pick: (c) => text(c.api_key) ?? text(c.oauth_token) ?? text(c.token),
  },
};

export class OneCliError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export class OneCliClient {
  constructor(
    private readonly baseUrl: string,
    private readonly fetchImpl: typeof fetch = fetch,
    private readonly apiKey?: string,
  ) {}

  async call(method: string, path: string, body?: unknown): Promise<any> {
    const res = await this.fetchImpl(this.baseUrl.replace(/\/$/, "") + "/api" + path, {
      method,
      headers: {
        "Content-Type": "application/json",
        ...(this.apiKey ? { Authorization: `Bearer ${this.apiKey}` } : {}),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const raw = await res.text();
    const data = raw ? JSON.parse(raw) : null;
    if (res.status >= 400) {
      // Never echo a request body: it can hold a credential.
      throw new OneCliError(
        res.status,
        `OneCLI ${method} ${path} failed: ${res.status} ${raw.slice(0, 200)}`,
      );
    }
    return data;
  }
}

export interface ApplyTarget {
  /** The OneCLI agent identifier NanoClaw uses: the agent group id. */
  identifier: string;
  /** Display name for the OneCLI agent if it has to be created. */
  name: string;
}

/** Make `lease` the one credential this agent group can use. Idempotent. */
export async function applyLease(
  onecli: OneCliClient,
  target: ApplyTarget,
  lease: AccountLease,
): Promise<void> {
  const mapping = PROVIDERS[lease.provider];
  if (!mapping) {
    throw new Error(
      `provider ${JSON.stringify(lease.provider)} cannot be applied to OneCLI (known: ${Object.keys(PROVIDERS).join(", ")})`,
    );
  }
  const value = mapping.pick(lease.credential);
  if (!value) {
    throw new Error(`account ${lease.name} has no usable credential field for ${lease.provider}`);
  }

  const agents: Array<{ id: string; identifier: string }> = await onecli.call("GET", "/agents");
  let agent = agents.find((a) => a.identifier === target.identifier);
  agent ??= await onecli.call("POST", "/agents", {
    name: target.name,
    identifier: target.identifier,
  });

  const secretName = `argus-${target.identifier}`;
  const secrets: Array<{ id: string; name: string }> = await onecli.call("GET", "/secrets");
  let secret = secrets.find((s) => s.name === secretName);
  if (secret) {
    await onecli.call("PATCH", `/secrets/${secret.id}`, { value });
  } else {
    secret = await onecli.call("POST", "/secrets", {
      name: secretName,
      type: mapping.type,
      value,
      hostPattern: mapping.host,
    });
  }
  await onecli.call("PUT", `/agents/${agent!.id}/secrets`, { secretIds: [secret!.id] });
  await onecli.call("PATCH", `/agents/${agent!.id}/secret-mode`, { mode: "selective" });
}

export type FailureKind =
  | { kind: "exhausted"; reason: string; until?: string }
  | { kind: "failed"; reason: string };

/**
 * Classify the text of an agent failure notice. NanoClaw's runner puts the
 * provider's own message there when a run was blocked ("Rate limit [five_hour]
 * (resets 2026-10-15T01:00:00.000Z)", "Out of credits"); anything else is a
 * plain failure. Matching is anchored: only those two provider messages count.
 */
export function classifyFailure(noticeText: string): FailureKind {
  const t = noticeText.trim();
  if (/^(rate limit|out of credits)\b/i.test(t)) {
    const m = /\(resets ([^)]+)\)/i.exec(t);
    const when = m ? new Date(m[1]!) : null;
    return {
      kind: "exhausted",
      reason: t.slice(0, 500),
      until: when && !Number.isNaN(when.getTime()) ? when.toISOString() : undefined,
    };
  }
  return { kind: "failed", reason: t.slice(0, 1000) || "the agent run failed" };
}
