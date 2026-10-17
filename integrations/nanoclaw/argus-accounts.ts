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
  /** `engine` (default): the harness's account; `executor`: a delegated CLI's. */
  role?: "engine" | "executor" | "repository";
}

/** How a provider's account becomes a OneCLI secret. */
interface ProviderMapping {
  type: string;
  /** The host the secret is injected for; may depend on the credential (API key vs login). */
  host: string | ((credential: Record<string, unknown>) => string);
  /** NanoClaw provider (the harness) that runs on this provider's accounts. */
  engine: string;
  /** Appended to the OneCLI secret name; empty for the first provider. */
  secretSuffix: string;
  /** The secret value out of an Argus credential document. */
  pick(credential: Record<string, unknown>): string | null;
  /** True when the credential is known to be past its own expiry (never logs a value). */
  expired?(credential: Record<string, unknown>, now: number): boolean;
  /** True when the picked value cannot be this provider's token (never logs a value). */
  malformed?(credential: Record<string, unknown>): boolean;
}

const text = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v.trim() : null);

/** Claude Code's own credentials file: `{ claudeAiOauth: { accessToken, expiresAt, … } }`. */
function claudeAiOauth(
  c: Record<string, unknown>,
): { accessToken?: unknown; expiresAt?: unknown } | null {
  const o = c.claudeAiOauth;
  return o && typeof o === "object" ? (o as Record<string, unknown>) : null;
}

/** Epoch milliseconds or an ISO date, whichever the document holds; null if neither. */
function expiryMs(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string" && v.trim()) {
    const t = /^\d+$/.test(v.trim()) ? Number(v) : Date.parse(v);
    return Number.isFinite(t) ? t : null;
  }
  return null;
}

/** Codex `auth.json` of a ChatGPT login: a token set. */
function chatgptLogin(c: Record<string, unknown>): boolean {
  const t = c.tokens;
  return (
    c.auth_mode === "chatgpt" &&
    !!t &&
    typeof t === "object" &&
    ["id_token", "access_token", "refresh_token"].every((k) =>
      text((t as Record<string, unknown>)[k]),
    )
  );
}

export const PROVIDERS: Record<string, ProviderMapping> = {
  // An API key, or a subscription's OAuth token; OneCLI tells them apart.
  anthropic: {
    type: "anthropic",
    host: "api.anthropic.com",
    engine: "claude",
    secretSuffix: "",
    pick: (c) =>
      text(c.api_key) ??
      text(c.oauth_token) ??
      text(c.token) ??
      text(claudeAiOauth(c)?.accessToken),
    // Only the Claude Code access token expires on its own: never apply one that has.
    expired: (c, now) => {
      if (text(c.api_key) ?? text(c.oauth_token) ?? text(c.token)) return false;
      const at = expiryMs(claudeAiOauth(c)?.expiresAt);
      return at !== null && at <= now;
    },
    // A subscription token is `sk-ant-oat…`; anything else there (say, the sign-in
    // page's `code#state`) would only surface as a 401 in the middle of a run.
    malformed: (c) => {
      if (text(c.api_key)) return false;
      const token = text(c.oauth_token) ?? text(c.token);
      return token !== null && !token.startsWith("sk-ant-oat");
    },
  },
  // Codex: a ChatGPT login (the whole auth.json, which OneCLI keeps fresh and
  // serves to the container as a sentinel file) or an API key.
  openai: {
    type: "openai",
    host: (c) => (chatgptLogin(c) ? "chatgpt.com" : "api.openai.com"),
    engine: "codex",
    secretSuffix: "-openai",
    pick: (c) =>
      chatgptLogin(c)
        ? JSON.stringify(c)
        : c.auth_mode === "apikey"
          ? text(c.OPENAI_API_KEY)
          : null,
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

/** Apply only a repository credential; never return it to the task/agent. */
export async function applyRepositoryLease(
  onecli: OneCliClient,
  target: ApplyTarget,
  lease: AccountLease,
  repositoryUrl: string,
  allowedRepositories: string[],
  activeLeases: AccountLease[],
): Promise<void> {
  if (lease.role !== "repository" || lease.provider !== "gitea")
    throw new Error("repository lease must use the gitea provider and repository role");
  const url = new URL(repositoryUrl);
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash)
    throw new Error("repository URL must be a plain HTTPS URL");
  const key = `${url.origin}${url.pathname.replace(/\/$/, "")}`;
  const allowed = allowedRepositories.some((raw) => {
    const candidate = new URL(raw);
    return (
      candidate.protocol === "https:" &&
      !candidate.username &&
      !candidate.password &&
      !candidate.search &&
      !candidate.hash &&
      `${candidate.origin}${candidate.pathname.replace(/\/$/, "")}` === key
    );
  });
  if (!allowed) throw new Error("repository URL is not in the configured allowlist");
  const token = text(lease.credential.token) ?? text(lease.credential.access_token);
  if (!token) throw new Error("gitea account has no token field");
  const username = text(lease.credential.username);
  if (!username || /[:\r\n]/.test(username) || /[\r\n]/.test(token))
    throw new Error("gitea account requires a valid username and token");
  try {
    const agents: Array<{ id: string; identifier: string }> = await onecli.call("GET", "/agents");
    let agent = agents.find((a) => a.identifier === target.identifier);
    agent ??= await onecli.call("POST", "/agents", {
      name: target.name,
      identifier: target.identifier,
    });
    const secrets: Array<{ id: string; name: string }> = await onecli.call("GET", "/secrets");
    const rootPath = url.pathname.replace(/\/$/, "");
    const secretBodies = [
      {
        name: `argus-${target.identifier}-repository-rest`,
        value: token,
        hostPattern: url.host,
        pathPattern: `/api/v1/repos${rootPath}/*`,
        injectionConfig: { headerName: "Authorization", valueFormat: "token {value}" },
      },
      {
        name: `argus-${target.identifier}-repository-git`,
        // Gitea smart-HTTP uses Basic auth. Build this only in the trusted host;
        // OneCLI persists it in its encrypted secret store.
        value: Buffer.from(`${username}:${token}`, "utf8").toString("base64"),
        hostPattern: url.host,
        pathPattern: `${rootPath}.git/*`,
        injectionConfig: { headerName: "Authorization", valueFormat: "Basic {value}" },
      },
    ];
    for (const secretBody of secretBodies) {
      let secret = secrets.find((s) => s.name === secretBody.name);
      if (secret) {
        await onecli.call("PATCH", `/secrets/${secret.id}`, { type: "generic", ...secretBody });
      } else {
        secret = await onecli.call("POST", "/secrets", { type: "generic", ...secretBody });
      }
    }
    await reconcileSecretAssignments(onecli, agent!.id, target.identifier, [
      ...activeLeases,
      lease,
    ]);
  } catch {
    // OneCLI error responses can contain request details. Never propagate them
    // where a caller might log the repository credential.
    throw new Error("could not apply repository credential to OneCLI");
  }
}

/** Unassign the repository secret so a released lease is no longer usable. */
export async function releaseRepositoryLease(
  onecli: OneCliClient,
  target: ApplyTarget,
  activeLeases: AccountLease[],
): Promise<void> {
  const agents: Array<{ id: string; identifier: string }> = await onecli.call("GET", "/agents");
  const agent = agents.find((a) => a.identifier === target.identifier);
  if (!agent) return;
  await reconcileSecretAssignments(onecli, agent.id, target.identifier, activeLeases);
}

/** Reconcile the complete currently-held lease set; GET /agents has no assignments. */
async function reconcileSecretAssignments(
  onecli: OneCliClient,
  agentId: string,
  identifier: string,
  leases: AccountLease[],
): Promise<void> {
  const secrets: Array<{ id: string; name: string }> = await onecli.call("GET", "/secrets");
  const names = new Set<string>();
  for (const lease of leases) {
    if (lease.role === "repository") continue;
    const mapping = PROVIDERS[lease.provider];
    if (!mapping) throw new Error("unsupported active lease provider");
    names.add(`argus-${identifier}${mapping.secretSuffix}`);
  }
  const repositoryNames = [
    `argus-${identifier}-repository-rest`,
    `argus-${identifier}-repository-git`,
  ];
  if (leases.some((lease) => lease.role === "repository"))
    repositoryNames.forEach((name) => names.add(name));
  const secretIds = [...names].map((name) => secrets.find((secret) => secret.name === name)?.id);
  if (secretIds.some((id) => !id)) throw new Error("active lease secret is missing from OneCLI");
  await onecli.call("PUT", `/agents/${agentId}/secrets`, { secretIds });
  await onecli.call("PATCH", `/agents/${agentId}/secret-mode`, { mode: "selective" });
}

export interface ApplyTarget {
  /** The OneCLI agent identifier NanoClaw uses: the agent group id. */
  identifier: string;
  /** Display name for the OneCLI agent if it has to be created. */
  name: string;
}

/** Validate a lease and turn its credential into what OneCLI stores. */
function secretFor(lease: AccountLease): {
  mapping: ProviderMapping;
  value: string;
  host: string;
} {
  const mapping = PROVIDERS[lease.provider];
  if (!mapping) {
    throw new Error(
      `provider ${JSON.stringify(lease.provider)} cannot be applied to OneCLI (known: ${Object.keys(PROVIDERS).join(", ")})`,
    );
  }
  if (mapping.expired?.(lease.credential, Date.now())) {
    throw new Error(
      `account ${lease.name} has an expired ${lease.provider} access token: renew it in Argus (the channel does not refresh OAuth tokens)`,
    );
  }
  if (mapping.malformed?.(lease.credential)) {
    throw new Error(
      `account ${lease.name} holds an ${lease.provider} credential that is not a token (expected sk-ant-oat…): renew it in Argus with the token from \`claude setup-token\``,
    );
  }
  const value = mapping.pick(lease.credential);
  if (!value) {
    throw new Error(`account ${lease.name} has no usable credential field for ${lease.provider}`);
  }
  const host = typeof mapping.host === "function" ? mapping.host(lease.credential) : mapping.host;
  return { mapping, value, host };
}

/**
 * Make `leases` exactly the credentials this agent group can use (first lease =
 * the engine's). Idempotent. A secret of an earlier lease that is not in the
 * list is unassigned, so a released lease leaves no usable residual secret.
 * Returns the NanoClaw provider (engine) that runs on the first lease's account.
 */
export async function applyLeases(
  onecli: OneCliClient,
  target: ApplyTarget,
  leases: AccountLease[],
): Promise<string> {
  if (leases.length === 0) throw new Error("applyLeases needs at least one lease");
  const prepared = leases
    .filter((lease) => lease.role !== "repository")
    .map((l) => ({ lease: l, ...secretFor(l) }));

  const agents: Array<{ id: string; identifier: string }> = await onecli.call("GET", "/agents");
  let agent = agents.find((a) => a.identifier === target.identifier);
  agent ??= await onecli.call("POST", "/agents", {
    name: target.name,
    identifier: target.identifier,
  });

  const secrets: Array<{ id: string; name: string }> = await onecli.call("GET", "/secrets");
  const ids: string[] = [];
  const done = new Set<string>();
  for (const { mapping, value, host } of prepared) {
    const secretName = `argus-${target.identifier}${mapping.secretSuffix}`;
    if (done.has(secretName)) continue; // one account per provider: the first lease wins
    done.add(secretName);
    let secret = secrets.find((s) => s.name === secretName);
    if (secret) {
      // A credential-dependent host (API key vs login) may change with the value.
      await onecli.call(
        "PATCH",
        `/secrets/${secret.id}`,
        typeof mapping.host === "function" ? { value, hostPattern: host } : { value },
      );
    } else {
      secret = await onecli.call("POST", "/secrets", {
        name: secretName,
        type: mapping.type,
        value,
        hostPattern: host,
      });
    }
    ids.push(secret!.id);
  }
  if (leases.some((lease) => lease.role === "repository")) {
    for (const suffix of ["rest", "git"]) {
      const name = `argus-${target.identifier}-repository-${suffix}`;
      const secret = secrets.find((item) => item.name === name);
      if (!secret) throw new Error("active repository lease secret is missing from OneCLI");
      ids.push(secret.id);
    }
  }
  await onecli.call("PUT", `/agents/${agent!.id}/secrets`, { secretIds: ids });
  await onecli.call("PATCH", `/agents/${agent!.id}/secret-mode`, { mode: "selective" });
  return prepared[0]!.mapping.engine;
}

/** The single-lease form of `applyLeases`. */
export const applyLease = (
  onecli: OneCliClient,
  target: ApplyTarget,
  lease: AccountLease,
): Promise<string> => applyLeases(onecli, target, [lease]);

export type FailureKind =
  | { kind: "exhausted"; reason: string; until?: string }
  | { kind: "failed"; reason: string };

/**
 * Quota messages by the provider whose run they end. Matching is anchored at
 * the start of the notice (after the runner's own "Reconnecting… n/m:" or
 * "Error:" prefix): a model that merely talks about limits never counts.
 */
const QUOTA_NOTICES: Array<{ provider: string; pattern: RegExp }> = [
  // Claude Code: "Rate limit [five_hour] (resets …)", "Out of credits".
  { provider: "anthropic", pattern: /^(rate limit|out of credits)\b/i },
  // Codex / ChatGPT: the error code, or the app's own sentence.
  {
    provider: "openai",
    pattern:
      /^(usage_limit_reached|insufficient_quota|you(?:'|\u2019)ve hit your usage limit|you have hit your usage limit)/i,
  },
];

/**
 * Classify the text of an agent failure notice. NanoClaw's runner puts the
 * provider's own message there when a run was blocked; anything else is a plain
 * failure. With `provider` (the engine account's), only that provider's quota
 * messages count, so a limit of another provider's account (a delegated
 * executor's, say) never exhausts the engine lease.
 */
export function classifyFailure(noticeText: string, provider?: string): FailureKind {
  const t = noticeText.trim();
  const body = t.replace(/^(?:error:\s*|reconnecting\.{3}\s*\d+\/\d+:\s*)/i, "");
  const hit = QUOTA_NOTICES.find(
    (q) => q.pattern.test(body) && (!provider || q.provider === provider),
  );
  if (hit) {
    const m = /\(resets ([^)]+)\)/i.exec(body);
    const when = m ? new Date(m[1]!) : null;
    return {
      kind: "exhausted",
      reason: t.slice(0, 500),
      until: when && !Number.isNaN(when.getTime()) ? when.toISOString() : undefined,
    };
  }
  return { kind: "failed", reason: t.slice(0, 1000) || "the agent run failed" };
}
