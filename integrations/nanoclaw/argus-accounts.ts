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
  /**
   * A reason, naming fields and never values, why the document cannot be applied
   * (wrong shape, expired). Checked before `pick`, with the time of the check.
   */
  problem?(credential: Record<string, unknown>, now: number): string | null;
  /**
   * The secret value when the document is not itself a usable token (an API key
   * is traded for an access token here, on the trusted host, so the worker's CLI
   * never makes the trade and never holds what it returns).
   */
  exchange?(credential: Record<string, unknown>, fetchImpl: typeof fetch): Promise<string | null>;
  /** No NanoClaw harness runs on this provider: only a delegated CLI uses it. */
  executorOnly?: boolean;
  /**
   * Inject with a generic header secret, one per endpoint (host + path pattern),
   * instead of a typed secret. A request that matches no endpoint is forwarded
   * untouched, so the credential cannot be used on any other path.
   */
  generic?: {
    headerName: string;
    valueFormat: string;
    endpoints: Array<{ suffix: string; pathPattern: string }>;
  };
}

const text = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v.trim() : null);

export interface NormalizedRepositoryUrl {
  origin: string;
  host: string;
  owner: string;
  repo: string;
  apiPath: string;
  gitPathPattern: string;
  cloneUrl: string;
}

/** Normalize repo and repo.git URLs to one API identity and one Git remote. */
export function normalizeRepositoryUrl(raw: string): NormalizedRepositoryUrl {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error("repository URL is invalid");
  }
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.pathname.endsWith("/")
  )
    throw new Error("repository URL must be a plain HTTPS repository URL");
  const parts = url.pathname.split("/").filter(Boolean);
  if (parts.length !== 2 || !parts.every((part) => /^[A-Za-z0-9_.-]+$/.test(part)))
    throw new Error("repository URL must identify exactly owner/repository");
  const [owner, rawRepo] = parts;
  const repo = rawRepo.endsWith(".git") ? rawRepo.slice(0, -4) : rawRepo;
  if (!repo) throw new Error("repository name must not be empty");
  const apiPath = `/api/v1/repos/${owner}/${repo}`;
  const gitPath = `/${owner}/${repo}.git`;
  return {
    origin: url.origin,
    host: url.host,
    owner,
    repo,
    apiPath,
    gitPathPattern: `${gitPath}/*`,
    cloneUrl: `${url.origin}${gitPath}`,
  };
}

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

/** `exp` of a JWT-shaped token in epoch ms, read without checking the signature; null if none. */
export function jwtExpiryMs(token: string): number | null {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const claims = JSON.parse(Buffer.from(parts[1]!, "base64url").toString("utf8"));
    const exp = claims?.exp;
    return typeof exp === "number" && Number.isFinite(exp) ? exp * 1000 : null;
  } catch {
    return null;
  }
}

/** The Cursor CLI treats an access token with under five minutes left as expiring. */
export const CURSOR_EXPIRY_MARGIN_MS = 300_000;
const CURSOR_HOST = "api2.cursor.sh";

/**
 * Why a Cursor document cannot be applied, or null. Two shapes, never mixed: an
 * `api_key`, or an imported login (`accessToken` + `refreshToken`). Only the
 * access token ever reaches the vault; a CLI settings file holds neither.
 */
function cursorProblem(c: Record<string, unknown>, now: number): string | null {
  const hasKey = "api_key" in c;
  const hasLogin = "accessToken" in c;
  if (hasKey && hasLogin) return "give only one of: api_key, accessToken";
  if (!hasKey && !hasLogin)
    return "has neither api_key nor accessToken with refreshToken (a Cursor settings file is not a credential)";
  if (hasKey) {
    const key = text(c.api_key);
    if (!key || /\s/.test(key)) return "api_key must be a non-empty string without whitespace";
    if (jwtExpiryMs(key) !== null)
      return "api_key holds a login token (a JWT); it belongs in accessToken with refreshToken";
    return null;
  }
  const access = text(c.accessToken);
  const missing = [access ? null : "accessToken", text(c.refreshToken) ? null : "refreshToken"];
  if (missing.some(Boolean))
    return `login credential is missing: ${missing.filter(Boolean).join(", ")}`;
  const exp = jwtExpiryMs(access!);
  if (exp === null) return "accessToken is not a JWT with an exp claim";
  if (exp <= now + CURSOR_EXPIRY_MARGIN_MS)
    return "accessToken is expired or about to expire (the channel does not refresh logins)";
  return null;
}

/**
 * Trade a Cursor API key for its access token, the way the CLI does
 * (`POST /auth/exchange_user_api_key`, key as bearer, observed on 2026.10.16),
 * but here on the trusted host. The refresh token in the answer is dropped.
 */
export async function exchangeCursorApiKey(
  apiKey: string,
  fetchImpl: typeof fetch = fetch,
): Promise<string> {
  let res: Response;
  try {
    res = await fetchImpl(`https://${CURSOR_HOST}/auth/exchange_user_api_key`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
      body: "{}",
      signal: AbortSignal.timeout(15_000),
    });
  } catch {
    throw new Error("the Cursor API key could not be exchanged (network error)");
  }
  if (!res.ok) throw new Error(`the Cursor API key was not accepted (HTTP ${res.status})`);
  let access: unknown;
  try {
    access = ((await res.json()) as { accessToken?: unknown }).accessToken;
  } catch {
    access = undefined;
  }
  const token = text(access);
  const exp = token ? jwtExpiryMs(token) : null;
  if (!token || exp === null || exp <= Date.now())
    throw new Error("the Cursor API key exchange returned no usable accessToken");
  return token;
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
  // Cursor: the CLI sends `Authorization: Bearer <token>` to api2.cursor.sh. The
  // container holds a sentinel token; the gateway swaps in the real access token
  // for exactly the paths below, over HTTP/1.1 (it does not negotiate h2). Observed
  // with the shipped CLI against a mock and the real gateway (cursor accounts spec).
  cursor: {
    type: "generic",
    host: CURSOR_HOST,
    engine: "",
    executorOnly: true,
    secretSuffix: "-cursor",
    generic: {
      headerName: "Authorization",
      valueFormat: "Bearer {value}",
      endpoints: [
        { suffix: "-agent", pathPattern: "/agent.v1.*" },
        { suffix: "-aiserver", pathPattern: "/aiserver.v1.*" },
      ],
    },
    problem: cursorProblem,
    pick: (c) => text(c.accessToken),
    exchange: async (c, fetchImpl) => {
      const key = text(c.api_key);
      return key ? exchangeCursorApiKey(key, fetchImpl) : null;
    },
  },
};

/**
 * Providers this runtime can technically apply for a lease role: capability of the
 * installed code (a mapping exists, a harness exists for an engine), never an
 * operator preference or order. Argus ranks the accounts of these globally.
 */
export function runtimeProviders(role: "engine" | "executor"): string[] {
  return Object.entries(PROVIDERS)
    .filter(([, mapping]) => role === "executor" || !mapping.executorOnly)
    .map(([provider]) => provider);
}

/** The delegate CLI (`argus-delegate EXECUTOR`) that runs on a provider's account. */
export function executorFor(provider: string): string | null {
  return { anthropic: "claude", openai: "codex", cursor: "agent" }[provider] ?? null;
}

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
  const repository = normalizeRepositoryUrl(repositoryUrl);
  const key = `${repository.origin}/${repository.owner}/${repository.repo}`;
  const allowed = allowedRepositories.some((raw) => {
    try {
      const candidate = normalizeRepositoryUrl(raw);
      return `${candidate.origin}/${candidate.owner}/${candidate.repo}` === key;
    } catch {
      return false;
    }
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
    const secretBodies = [
      {
        name: `argus-${target.identifier}-repository-rest`,
        value: token,
        hostPattern: repository.host,
        pathPattern: `${repository.apiPath}/*`,
        injectionConfig: { headerName: "Authorization", valueFormat: "token {value}" },
      },
      {
        name: `argus-${target.identifier}-repository-git`,
        // Gitea smart-HTTP uses Basic auth. Build this only in the trusted host;
        // OneCLI persists it in its encrypted secret store.
        value: Buffer.from(`${username}:${token}`, "utf8").toString("base64"),
        hostPattern: repository.host,
        pathPattern: repository.gitPathPattern,
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
    secretNames(identifier, mapping).forEach((name) => names.add(name));
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

/** The OneCLI secret names a provider's account occupies for one agent group. */
function secretNames(identifier: string, mapping: ProviderMapping): string[] {
  const base = `argus-${identifier}${mapping.secretSuffix}`;
  return mapping.generic ? mapping.generic.endpoints.map((e) => `${base}${e.suffix}`) : [base];
}

/** Validate a lease and turn its credential into what OneCLI stores. */
async function secretFor(
  lease: AccountLease,
  fetchImpl: typeof fetch,
): Promise<{ mapping: ProviderMapping; value: string; host: string }> {
  const mapping = PROVIDERS[lease.provider];
  if (!mapping) {
    throw new Error(
      `provider ${JSON.stringify(lease.provider)} cannot be applied to OneCLI (known: ${Object.keys(PROVIDERS).join(", ")})`,
    );
  }
  const problem = mapping.problem?.(lease.credential, Date.now());
  if (problem) throw new Error(`account ${lease.name} (${lease.provider}): ${problem}`);
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
  const value =
    mapping.pick(lease.credential) ?? (await mapping.exchange?.(lease.credential, fetchImpl));
  if (!value) {
    throw new Error(`account ${lease.name} has no usable credential field for ${lease.provider}`);
  }
  const host = typeof mapping.host === "function" ? mapping.host(lease.credential) : mapping.host;
  return { mapping, value, host };
}

export interface AppliedLeases {
  /** The NanoClaw provider (engine) that runs on the first lease's account. */
  engine: string;
  /** When the value injected for the `executor` lease stops working (epoch ms), if it says. */
  executorExpiresAt: number | null;
}

/**
 * Make `leases` exactly the credentials this agent group can use (first lease =
 * the engine's). Idempotent. A secret of an earlier lease that is not in the
 * list is unassigned, so a released lease leaves no usable residual secret.
 */
export async function applyLeasesDetailed(
  onecli: OneCliClient,
  target: ApplyTarget,
  leases: AccountLease[],
  fetchImpl: typeof fetch = fetch,
): Promise<AppliedLeases> {
  if (leases.length === 0) throw new Error("applyLeases needs at least one lease");
  const prepared = [];
  for (const lease of leases.filter((l) => l.role !== "repository")) {
    prepared.push({ lease, ...(await secretFor(lease, fetchImpl)) });
  }
  if (prepared[0]?.mapping.executorOnly)
    throw new Error(`provider ${prepared[0].lease.provider} is an executor, not an engine`);

  const agents: Array<{ id: string; identifier: string }> = await onecli.call("GET", "/agents");
  let agent = agents.find((a) => a.identifier === target.identifier);
  agent ??= await onecli.call("POST", "/agents", {
    name: target.name,
    identifier: target.identifier,
  });

  const secrets: Array<{ id: string; name: string }> = await onecli.call("GET", "/secrets");
  const ids: string[] = [];
  const done = new Set<string>();
  let executorExpiresAt: number | null = null;
  for (const { lease, mapping, value, host } of prepared) {
    const names = secretNames(target.identifier, mapping);
    if (done.has(names[0]!)) continue; // one account per provider: the first lease wins
    done.add(names[0]!);
    if (lease.role === "executor") executorExpiresAt = jwtExpiryMs(value);
    for (const [index, secretName] of names.entries()) {
      const endpoint = mapping.generic?.endpoints[index];
      const body = endpoint
        ? {
            name: secretName,
            type: "generic",
            value,
            hostPattern: host,
            pathPattern: endpoint.pathPattern,
            injectionConfig: {
              headerName: mapping.generic!.headerName,
              valueFormat: mapping.generic!.valueFormat,
            },
          }
        : { name: secretName, type: mapping.type, value, hostPattern: host };
      let secret = secrets.find((s) => s.name === secretName);
      if (secret) {
        // A credential-dependent host (API key vs login) may change with the value;
        // a generic secret is always sent whole so its host, path and header are ours.
        await onecli.call(
          "PATCH",
          `/secrets/${secret.id}`,
          endpoint
            ? body
            : typeof mapping.host === "function"
              ? { value, hostPattern: host }
              : { value },
        );
      } else {
        secret = await onecli.call("POST", "/secrets", body);
      }
      ids.push(secret!.id);
    }
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
  return { engine: prepared[0]!.mapping.engine, executorExpiresAt };
}

/** `applyLeasesDetailed`, returning only the engine (the NanoClaw provider to run). */
export async function applyLeases(
  onecli: OneCliClient,
  target: ApplyTarget,
  leases: AccountLease[],
  fetchImpl: typeof fetch = fetch,
): Promise<string> {
  return (await applyLeasesDetailed(onecli, target, leases, fetchImpl)).engine;
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
  // Claude Code: "Rate limit [five_hour] (resets …)", "Out of credits",
  // "You've hit your session limit · resets 11:50pm (UTC)", "Usage limit reached".
  {
    provider: "anthropic",
    pattern:
      /^(rate limit|out of credits|usage limit reached|you(?:'|\u2019)ve hit your (?:\w+ )?limit|you have hit your (?:\w+ )?limit)\b/i,
  },
  // Codex / ChatGPT: the error code, or the app's own sentence.
  {
    provider: "openai",
    pattern:
      /^(usage_limit_reached|insufficient_quota|you(?:'|\u2019)ve hit your usage limit|you have hit your usage limit)/i,
  },
];

/** OpenAI engine auth expiry (401/token_expired): treated as an exhausted account so Argus leases another one. */
const AUTH_EXPIRED_NOTICES: Array<{ provider: string; pattern: RegExp }> = [
  {
    provider: "openai",
    pattern:
      /^(?:error:\s*)?(?:http\s*)?401\b.*token_expired|^(?:error:\s*)?token_expired\b|^(?:error:\s*)?(?:http\s*)?401\b.*(?:authentication|unauthori[sz]ed)/i,
  },
];

/**
 * When a quota notice says the limit resets: "(resets <ISO>)" or a UTC clock time
 * ("resets 11:50pm (UTC)", the next such time strictly after `now`). Undefined
 * when absent or unparsable.
 */
function parseResetTime(body: string, now: Date): string | undefined {
  const iso = /\(resets ([^)]+)\)/i.exec(body);
  if (iso) {
    const when = new Date(iso[1]!);
    if (!Number.isNaN(when.getTime())) return when.toISOString();
  }
  const clock = /\bresets\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\s*\(UTC\)/i.exec(body);
  if (!clock) return undefined;
  const hour12 = Number(clock[1]);
  const minute = Number(clock[2] ?? "0");
  if (hour12 < 1 || hour12 > 12 || minute > 59) return undefined;
  const hour = (hour12 % 12) + (clock[3]!.toLowerCase() === "p" ? 12 : 0);
  const at = new Date(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), hour, minute),
  );
  if (at.getTime() <= now.getTime()) at.setUTCDate(at.getUTCDate() + 1);
  return at.toISOString();
}

/**
 * Classify the text of an agent failure notice. NanoClaw's runner puts the
 * provider's own message there when a run was blocked; anything else is a plain
 * failure. With `provider` (the engine account's), only that provider's quota
 * messages count, so a limit of another provider's account (a delegated
 * executor's, say) never exhausts the engine lease.
 */
export function classifyFailure(
  noticeText: string,
  provider?: string,
  now: Date = new Date(),
): FailureKind {
  const t = noticeText.trim();
  const body = t.replace(/^(?:error:\s*|reconnecting\.{3}\s*\d+\/\d+:\s*)/i, "");
  const authHit = AUTH_EXPIRED_NOTICES.find(
    (q) => q.pattern.test(body) && (!provider || q.provider === provider),
  );
  if (authHit) {
    return {
      kind: "exhausted",
      reason: t.slice(0, 500),
      until: undefined,
    };
  }
  const hit = QUOTA_NOTICES.find(
    (q) => q.pattern.test(body) && (!provider || q.provider === provider),
  );
  if (hit) {
    return { kind: "exhausted", reason: t.slice(0, 500), until: parseResetTime(body, now) };
  }
  return { kind: "failed", reason: t.slice(0, 1000) || "the agent run failed" };
}

/** Fixed, non-sensitive reason for a structured auth expiry; never the notice or log text. */
const STRUCTURED_AUTH_EXPIRED_REASON = "OpenAI/Codex authentication expired";

/** The runner's placeholder when the provider's own message did not reach the notice. */
export function isGenericFailureNotice(noticeText: string): boolean {
  return /^the agent run failed\b/i.test(noticeText.trim());
}

/** `Rate limit reached`, plus the reset when it is a valid timestamp. Starts like Claude Code's own notice so Argus's server-side patterns match it too. */
function rateLimitReason(resetsAt: unknown): { reason: string; until?: string } {
  const when = typeof resetsAt === "string" ? new Date(resetsAt) : null;
  if (!when || Number.isNaN(when.getTime())) return { reason: "Rate limit reached" };
  const until = when.toISOString();
  return { reason: `Rate limit reached (resets ${until})`, until };
}

/**
 * The structured `content.providerFailure` of a NanoClaw failure notice
 * (docs/specs/codex-accounts.spec.md §3, account-selection.spec.md 3.6).
 * `{provider:"codex", kind:"auth_expired"}` against a held `openai` account and
 * `{provider:"claude", kind:"rate_limit", resets_at?}` against a held `anthropic`
 * account exhaust it (`codex`/`claude` are the NanoClaw engines; Argus names the
 * accounts by their vendor). Anything else (unknown value, other account
 * provider, bad shape) is not a decision: null. Reasons are fixed, never the raw text.
 */
export function classifyProviderFailure(
  value: unknown,
  accountProvider?: string,
): FailureKind | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const { provider, kind, resets_at } = value as {
    provider?: unknown;
    kind?: unknown;
    resets_at?: unknown;
  };
  if (accountProvider === "openai" && provider === "codex" && kind === "auth_expired") {
    return { kind: "exhausted", reason: STRUCTURED_AUTH_EXPIRED_REASON, until: undefined };
  }
  if (accountProvider === "anthropic" && provider === "claude" && kind === "rate_limit") {
    return { kind: "exhausted", ...rateLimitReason(resets_at) };
  }
  return null;
}

/**
 * Claude Code's own limit line in the agent log tail, for runs whose notice is
 * only the generic placeholder: `Rate limit [window] (resets <ISO>)` or `You've
 * hit your session|usage|weekly limit · resets 2am (UTC)` (the next such UTC time
 * after `now`). The last occurrence wins; only a held `anthropic` account is
 * affected. The reason is rebuilt from the parsed reset, never the log text.
 */
export function classifyLogTail(
  logTail: string,
  accountProvider?: string,
  now: Date = new Date(),
): FailureKind | null {
  if (accountProvider !== "anthropic") return null;
  const hits = [
    ...logTail.matchAll(
      /Rate limit \[[^\]\n]*\](?: \(resets ([^)\n]+)\))?|You(?:'|\u2019)ve hit your (?:session|usage|weekly) limit · (resets [^\n"]+)/gi,
    ),
  ];
  const last = hits.at(-1);
  if (!last) return null;
  return {
    kind: "exhausted",
    ...rateLimitReason(last[2] ? parseResetTime(last[2], now) : last[1]),
  };
}
