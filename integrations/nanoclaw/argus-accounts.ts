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

import { localCredentialProblem } from "./argus-local-harness.js";

export interface CustomLeaseSettings {
  base_url: string;
  label: string | null;
  model: string | null;
  provider: Record<string, unknown> | null;
}

export interface AccountLease {
  lease_id: string;
  account_id: string;
  name: string;
  provider: string;
  kind: string;
  credential: Record<string, unknown>;
  /** `engine` (default): the harness's account; `executor`: a delegated CLI's. */
  role?: "engine" | "executor" | "repository";
  /** Custom accounts: where and how to call for this role (no secret). */
  settings?: CustomLeaseSettings | null;
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
  /** Engine lease env is applied on the host (local Strata); no OneCLI secret row. */
  harnessEnvOnly?: boolean;
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

function customCredentialProblem(credential: Record<string, unknown>): string | null {
  const keys = new Set(Object.keys(credential));
  if (keys.size !== 1 || !keys.has("api_key")) return "custom credential must be exactly {api_key}";
  const key = text(credential.api_key);
  if (!key) return "custom credential needs a non-empty api_key string";
  if (/\s|[\r\n\u0000]/.test(key))
    return "api_key must not contain whitespace or control characters";
  return null;
}

export interface NormalizedRepositoryUrl {
  /** github.com is GitHub; every other host is Gitea (GitHub repositories spec 3.1). */
  provider: "gitea" | "github";
  origin: string;
  host: string;
  owner: string;
  repo: string;
  /** Host of the REST API: the forge itself for Gitea, api.github.com for GitHub. */
  apiHost: string;
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
  const github = url.host === "github.com";
  const apiPath = github ? `/repos/${owner}/${repo}` : `/api/v1/repos/${owner}/${repo}`;
  const gitPath = `/${owner}/${repo}.git`;
  return {
    provider: github ? "github" : "gitea",
    origin: url.origin,
    host: url.host,
    owner,
    repo,
    apiHost: github ? "api.github.com" : url.host,
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
  local: {
    type: "generic",
    host: "local",
    engine: "claude",
    secretSuffix: "-local",
    harnessEnvOnly: true,
    problem: (c) => localCredentialProblem(c),
    pick: (c) => JSON.stringify(c),
  },
  custom: {
    type: "openai",
    host: "custom-harness",
    engine: "claude",
    secretSuffix: "-custom",
    harnessEnvOnly: true,
    problem: (c) => customCredentialProblem(c),
    pick: (c) => text(c.api_key),
  },
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

/**
 * Recommended delegated CLI: that of the executor lease actually held, otherwise the
 * engine's own. A local or custom engine serves only the harness (no CLI of its own),
 * so with no executor leased the answer is null: never an unleased Cursor. Claude Code
 * never runs on the local model (the delegate refuses it), so it is not recommended there.
 */
export function preferredExecutorForLease(
  engine: Pick<AccountLease, "provider"> | null,
  executor: Pick<AccountLease, "provider"> | null,
): string | null {
  if (executor) {
    return engine?.provider === "local" && executor.provider === "anthropic"
      ? null
      : executorFor(executor.provider);
  }
  return engine ? executorFor(engine.provider) : null;
}

/**
 * The executor provider leased first on a harness that only orchestrates: Cursor on a
 * local (Strata) or custom (bridge) engine lease, so `agent` has its stub (local and
 * custom harness specs). Null: the ordinary executor lease across providers.
 */
export function executorFirst(engineProvider: string | null | undefined): string | null {
  return engineProvider === "local" || engineProvider === "custom" ? "cursor" : null;
}

export class OneCliError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

const ONECLI_HTTP_HOSTS = new Set([
  "localhost",
  "127.0.0.1",
  "::1",
  "[::1]",
  "host.docker.internal",
]);

/** Private Docker DNS for the OneCLI admin service (e.g. on `onecli-control`). */
const ONECLI_DOCKER_SERVICE_HOST = "onecli";
const ONECLI_DOCKER_SERVICE_PORT = 10254;
const DOCKER_BUILTIN_NETWORKS = new Set(["bridge", "default", "host", "none"]);

export type OneCliUrlContext = {
  /** User-defined Docker network name (mirrors pool `docker.network` / `ARGUS_NANOCLAW_NETWORK`). */
  dockerNetwork?: string | null;
};

function dockerUserDefinedNetworkOk(network: string | null | undefined): boolean {
  const name = (network ?? "").trim();
  if (!name) return false;
  return !DOCKER_BUILTIN_NETWORKS.has(name.toLowerCase());
}

function onecliPrivateDockerServiceHttpOk(parsed: URL): boolean {
  if (parsed.protocol !== "http:") return false;
  if (parsed.username || parsed.password) return false;
  if (parsed.hostname.toLowerCase() !== ONECLI_DOCKER_SERVICE_HOST) return false;
  const port = parsed.port === "" ? 80 : Number(parsed.port);
  if (port !== ONECLI_DOCKER_SERVICE_PORT) return false;
  const path = parsed.pathname;
  if (path !== "/" && path !== "") return false;
  if (parsed.search || parsed.hash) return false;
  return true;
}

/** Mirror of Argus `NanoclawOptions.onecli_url` (security-hardening spec). */
export function assertOneCliUrl(url: string, context?: OneCliUrlContext): string {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error(`invalid OneCLI URL: ${url}`);
  }
  if (parsed.username || parsed.password) {
    throw new Error("OneCLI URL must not embed credentials");
  }
  const host = parsed.hostname.toLowerCase();
  if (parsed.protocol === "https:") {
    if (!host) throw new Error("OneCLI URL needs a host");
    return url;
  }
  if (parsed.protocol === "http:" && ONECLI_HTTP_HOSTS.has(host)) {
    return url;
  }
  if (onecliPrivateDockerServiceHttpOk(parsed)) {
    const network = context?.dockerNetwork ?? process.env.ARGUS_NANOCLAW_NETWORK;
    if (!dockerUserDefinedNetworkOk(network)) {
      throw new Error(
        "http://onecli:10254 requires ARGUS_NANOCLAW_NETWORK to be a user-defined Docker network (not bridge, default, host, or none; e.g. onecli-control)",
      );
    }
    return url;
  }
  throw new Error(
    "OneCLI URL must be https://, or http:// to localhost / 127.0.0.1 / host.docker.internal / http://onecli:10254 on a private Docker network",
  );
}

export class OneCliClient {
  constructor(
    private readonly baseUrl: string,
    private readonly fetchImpl: typeof fetch = fetch,
    private readonly apiKey?: string,
  ) {
    assertOneCliUrl(baseUrl, { dockerNetwork: process.env.ARGUS_NANOCLAW_NETWORK });
  }

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
      // Never echo a response body by default: it can hold secrets or PII.
      const debug = process.env.ARGUS_ONECLI_DEBUG_BODY === "1";
      let code = "error";
      if (data && typeof data === "object" && "error" in data) {
        const err = (data as { error?: unknown }).error;
        if (typeof err === "string" && err.length <= 64) code = err;
        else if (err && typeof err === "object" && "code" in err) {
          const c = (err as { code?: unknown }).code;
          if (typeof c === "string" && c.length <= 64) code = c;
        }
      }
      const detail = debug ? ` ${raw.slice(0, 200)}` : ` ${code}`;
      throw new OneCliError(res.status, `OneCLI ${method} ${path} failed: ${res.status}${detail}`);
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
  const repository = normalizeRepositoryUrl(repositoryUrl);
  if (lease.role !== "repository" || lease.provider !== repository.provider)
    throw new Error(
      `repository lease must use the ${repository.provider} provider and repository role`,
    );
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
  if (!token) throw new Error(`${repository.provider} account has no token field`);
  // GitHub takes any Git username with a token; x-access-token is the documented one.
  const username =
    repository.provider === "github" ? "x-access-token" : text(lease.credential.username);
  if (!username || /[:\r\n]/.test(username) || /[\r\n]/.test(token))
    throw new Error(`${repository.provider} account requires a valid username and token`);
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
        hostPattern: repository.apiHost,
        pathPattern: `${repository.apiPath}/*`,
        injectionConfig: {
          headerName: "Authorization",
          valueFormat: repository.provider === "github" ? "Bearer {value}" : "token {value}",
        },
      },
      {
        name: `argus-${target.identifier}-repository-git`,
        // Smart-HTTP uses Basic auth on both forges. Build this only in the trusted host;
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
    // A harness-env-only account (local Strata) has no OneCLI secret.
    if (mapping.harnessEnvOnly) continue;
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
    if (mapping.harnessEnvOnly) continue;
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

/** `stated`: the provider gave a time with a zone; `ambiguous`: it named one we cannot place; `absent`: none. */
export type ResetStatus = "stated" | "ambiguous" | "absent";
export type QuotaCode = "quota_exhausted" | "auth_expired";
export type FailureSource = "structured" | "notice" | "log";

export type FailureKind =
  | {
      kind: "exhausted";
      reason: string;
      until?: string;
      code?: QuotaCode;
      resetStatus?: ResetStatus;
      /** The provider's own words about the reset, bounded; never a guessed time. */
      resetText?: string;
      source?: FailureSource;
    }
  | { kind: "failed"; reason: string };

/** Control characters folded to spaces, whitespace collapsed: the text classified (URLs and all, so a JSON envelope still parses). */
function foldNotice(notice: string): string {
  return notice
    .replace(/[\x00-\x1f\x7f]+/g, " ")
    .replace(/\s{2,}/g, " ")
    .trim();
}

/**
 * One line of provider text: control characters to spaces, bearer-like tokens and
 * URL query strings removed, bounded. Never a prompt or a credential. Mirrors
 * `argus_core.quota.sanitize_notice` (both are tested on one shared fixture).
 */
export function sanitizeNotice(notice: string, limit = 500): string {
  return foldNotice(notice)
    .replace(/\b(?:bearer\s+|sk-|eyJ)[A-Za-z0-9._~+/=-]{8,}/gi, "[redacted]")
    .replace(/(https?:\/\/[^\s?#)]+)[?#][^\s)]*/g, "$1")
    .replace(/\s{2,}/g, " ")
    .trim()
    .slice(0, limit);
}

/** Structured Codex error codes that mean "this account's quota is spent". */
const STRUCTURED_QUOTA_CODES = new Set([
  "usagelimitexceeded",
  "usage_limit_exceeded",
  "usage_limit_reached",
  "insufficient_quota",
]);

const NOTICE_PREFIX =
  /^(?:(?:codex |claude )?error:\s*|turn failed:\s*|reconnecting\.{3}\s*\d+\/\d+:\s*)/i;

/** A structured Codex error as the text its code or message would have been. */
function unwrapStructured(body: string): string {
  if (!body.startsWith("{")) return body;
  let doc: unknown;
  try {
    doc = JSON.parse(body);
  } catch {
    return body;
  }
  if (typeof doc !== "object" || doc === null || Array.isArray(doc)) return body;
  const top = doc as Record<string, unknown>;
  const err = "error" in top ? top.error : top;
  if (typeof err === "string") return err;
  if (typeof err !== "object" || err === null || Array.isArray(err)) return body;
  const fields = err as Record<string, unknown>;
  const message = typeof fields.message === "string" ? fields.message : undefined;
  for (const key of ["codexErrorInfo", "code", "type"]) {
    const value = fields[key];
    if (typeof value === "string" && STRUCTURED_QUOTA_CODES.has(value.toLowerCase())) {
      return message !== undefined ? `${value}: ${message}` : value;
    }
  }
  return message ?? body;
}

/** The text of a notice with the runner's own prefixes (and a JSON wrapper) removed. */
function noticeBody(sanitized: string): string {
  return unwrapStructured(sanitized.replace(NOTICE_PREFIX, "")).replace(NOTICE_PREFIX, "");
}

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
      /^(usage_limit_(?:reached|exceeded)|usagelimitexceeded|insufficient_quota|you(?:'|\u2019)ve (?:hit|reached) your usage limit|you have (?:hit|reached) your usage limit|you exceeded your current quota)/i,
  },
  {
    provider: "custom",
    pattern: /^(?:http\s*)?(?:402|429)\b|^(?:insufficient credits|payment required|rate limit)/i,
  },
];

/** Engine auth expiry (401/token_expired): treated as an exhausted account so Argus leases another one. */
const AUTH_EXPIRED_PATTERN =
  /^(?:error:\s*)?(?:http\s*)?401\b.*token_expired|^(?:error:\s*)?token_expired\b|^(?:error:\s*)?(?:http\s*)?401\b.*(?:authentication|unauthori[sz]ed)/i;

const AUTH_EXPIRED_NOTICES: Array<{ provider: string; pattern: RegExp }> = [
  { provider: "openai", pattern: AUTH_EXPIRED_PATTERN },
  { provider: "custom", pattern: AUTH_EXPIRED_PATTERN },
];

const RESET_MARKER = /\(resets |\bresets? (?:at |on )?|try again (?:at|in) /i;

/** An ISO time counts only with a zone (`Z` or an offset); a bare one is the provider's local time. */
function parseZonedIso(text: string): string | undefined {
  const t = text.trim();
  if (!/(?:Z|[+-]\d{2}:?\d{2})$/i.test(t)) return undefined;
  const when = new Date(t);
  return Number.isNaN(when.getTime()) ? undefined : when.toISOString();
}

/**
 * When a quota notice says the limit resets: "(resets <ISO with zone>)" or a UTC
 * clock time ("resets 11:50pm (UTC)", the next such time strictly after `now`).
 * Undefined when absent, zoneless or unparsable.
 */
function parseResetTime(body: string, now: Date): string | undefined {
  const iso = /\(resets ([^)]+)\)/i.exec(body);
  if (iso) {
    const when = parseZonedIso(iso[1]!);
    if (when) return when;
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

interface ResetInfo {
  until?: string;
  resetStatus: ResetStatus;
  resetText?: string;
}

/** The reset a notice states: its time only with a zone, else the provider's words (`ambiguous`) or nothing (`absent`). */
function parseReset(body: string, now: Date): ResetInfo {
  const until = parseResetTime(body, now);
  const marker = RESET_MARKER.exec(body);
  let resetText: string | undefined;
  if (marker) {
    let rest = body.slice(marker.index + marker[0].length).split("\n")[0]!;
    if (marker[0].startsWith("(")) rest = rest.split(")")[0]!;
    rest = rest.split(/\.(?:\s|$)/)[0]!;
    resetText = sanitizeNotice(rest, 80).replace(/\.+$/, "") || undefined;
  }
  if (until) return { until, resetStatus: "stated", resetText };
  return { resetStatus: resetText ? "ambiguous" : "absent", resetText };
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
  const t = sanitizeNotice(noticeText);
  const body = noticeBody(foldNotice(noticeText));
  const authHit = AUTH_EXPIRED_NOTICES.find(
    (q) => q.pattern.test(body) && (!provider || q.provider === provider),
  );
  if (authHit) {
    return {
      kind: "exhausted",
      reason: t,
      code: "auth_expired",
      resetStatus: "absent",
      source: "notice",
    };
  }
  const hit = QUOTA_NOTICES.find(
    (q) => q.pattern.test(body) && (!provider || q.provider === provider),
  );
  if (hit) {
    return {
      kind: "exhausted",
      reason: t,
      code: "quota_exhausted",
      source: "notice",
      ...parseReset(body, now),
    };
  }
  return { kind: "failed", reason: sanitizeNotice(noticeText, 1000) || "the agent run failed" };
}

/**
 * What a run that ended with only the generic placeholder said last: the final
 * non-empty lines of the (already redacted) agent log, sanitized and bounded, or
 * null when the log holds nothing.
 */
export function describeLogTail(logTail: string, lines = 3, limit = 300): string | null {
  const last = logTail
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(-lines)
    .join(" | ");
  const text = sanitizeNotice(last, limit);
  return text || null;
}

/** Fixed, non-sensitive reason for a structured auth expiry; never the notice or log text. */
const STRUCTURED_AUTH_EXPIRED_REASON = "OpenAI/Codex authentication expired";
const STRUCTURED_CODEX_LIMIT_REASON = "Codex usage limit reached";

/** The runner's placeholder when the provider's own message did not reach the notice. */
export function isGenericFailureNotice(noticeText: string): boolean {
  return /^the agent run failed\b/i.test(noticeText.trim());
}

/** `Rate limit reached`, plus the reset when it is a valid timestamp. Starts like Claude Code's own notice so Argus's server-side patterns match it too. */
function rateLimitReason(resetsAt: unknown): {
  reason: string;
  until?: string;
  resetStatus: ResetStatus;
} {
  const when = typeof resetsAt === "string" ? new Date(resetsAt) : null;
  if (!when || Number.isNaN(when.getTime())) {
    return { reason: "Rate limit reached", resetStatus: "absent" };
  }
  const until = when.toISOString();
  return { reason: `Rate limit reached (resets ${until})`, until, resetStatus: "stated" };
}

/**
 * The structured `content.providerFailure` of a NanoClaw failure notice
 * (docs/specs/codex-accounts.spec.md §3, account-selection.spec.md 3.6).
 * `{provider:"codex", kind:"auth_expired"}` and `{provider:"codex",
 * kind:"rate_limit", notice?, reset_text?, resets_at?}` against a held `openai`
 * account, and `{provider:"claude", kind:"rate_limit", resets_at?}` against a held
 * `anthropic` account exhaust it (`codex`/`claude` are the NanoClaw engines; Argus
 * names the accounts by their vendor). Anything else (unknown value, other account
 * provider, bad shape) is not a decision: null. The reason is the sanitized notice
 * when given, else a fixed text; a reset counts only with a zone.
 */
export function classifyProviderFailure(
  value: unknown,
  accountProvider?: string,
  now: Date = new Date(),
): FailureKind | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const { provider, kind, resets_at, notice, reset_text } = value as {
    provider?: unknown;
    kind?: unknown;
    resets_at?: unknown;
    notice?: unknown;
    reset_text?: unknown;
  };
  if (accountProvider === "openai" && provider === "codex" && kind === "auth_expired") {
    return {
      kind: "exhausted",
      reason: STRUCTURED_AUTH_EXPIRED_REASON,
      until: undefined,
      code: "auth_expired",
      resetStatus: "absent",
      source: "structured",
    };
  }
  if (accountProvider === "openai" && provider === "codex" && kind === "rate_limit") {
    const text = typeof notice === "string" ? sanitizeNotice(notice) : "";
    let reset: ResetInfo = { resetStatus: "absent" };
    const at = typeof resets_at === "string" ? parseZonedIso(resets_at) : undefined;
    if (at) reset = { until: at, resetStatus: "stated" };
    else if (text) reset = parseReset(noticeBody(text), now);
    if (!reset.resetText && typeof reset_text === "string") {
      const words = sanitizeNotice(reset_text, 80);
      if (words) {
        reset = { ...reset, resetText: words };
        if (reset.resetStatus === "absent") reset.resetStatus = "ambiguous";
      }
    }
    return {
      kind: "exhausted",
      reason: text || STRUCTURED_CODEX_LIMIT_REASON,
      code: "quota_exhausted",
      source: "structured",
      ...reset,
    };
  }
  if (accountProvider === "anthropic" && provider === "claude" && kind === "rate_limit") {
    return {
      kind: "exhausted",
      code: "quota_exhausted",
      source: "structured",
      ...rateLimitReason(resets_at),
    };
  }
  return null;
}

/** Timestamp and `[level]` tags a log line may open with, before the error text. */
const LOG_LINE_LEAD = /^(?:\[[^\]]*\]\s*|\d{4}-\d\d-\d\d[T ]\S+\s+)+/;

/**
 * The provider's own limit line in the agent log tail, for runs whose notice is
 * only the generic placeholder. Claude Code: `Rate limit [window] (resets <ISO>)` or
 * `You've hit your session|usage|weekly limit · resets 2am (UTC)` (the next such UTC
 * time after `now`). Codex: an error line (or structured error) carrying the usage-limit sentence or code. The last
 * occurrence wins; only the held account's own provider is affected. The reason is
 * rebuilt from the parsed reset (Claude) or sanitized (Codex), never the raw log.
 */
export function classifyLogTail(
  logTail: string,
  accountProvider?: string,
  now: Date = new Date(),
): FailureKind | null {
  if (accountProvider === "openai") {
    // Only a line that is itself a Codex error ("Error: …", "Turn failed: …", a
    // structured error envelope): the same words in a prompt or tool output never count.
    for (const line of logTail.split("\n").reverse()) {
      const text = line.trim().replace(LOG_LINE_LEAD, "");
      const envelope = text.startsWith("{") && /^\{\s*"error"\s*:|"codexErrorInfo"/.test(text);
      if (!envelope && !NOTICE_PREFIX.test(text)) continue;
      const found = classifyFailure(text, "openai", now);
      if (found.kind === "exhausted") return { ...found, source: "log" };
    }
    return null;
  }
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
    code: "quota_exhausted",
    source: "log",
    ...rateLimitReason(last[2] ? parseResetTime(last[2], now) : last[1]),
  };
}
