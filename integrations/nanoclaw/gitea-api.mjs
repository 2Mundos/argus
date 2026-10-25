#!/usr/bin/env node
// Credential-free Gitea helper for workers (argus-gitea-api). Spec: docs/specs/gitea-helper.spec.md.
import { createHash, randomBytes } from "node:crypto";
import {
  closeSync,
  fsyncSync,
  linkSync,
  lstatSync,
  openSync,
  readFileSync,
  renameSync,
  statSync,
  unlinkSync,
  writeSync,
} from "node:fs";
import { createRequire } from "node:module";
import { basename, dirname, join, resolve } from "node:path";

const require = createRequire(import.meta.url);

function loadUndici() {
  try {
    return require("undici");
  } catch {
    // Installed into a dedicated, pinned runtime location in the agent image.
    return createRequire("/usr/local/lib/argus-gitea/package.json")("undici");
  }
}

/** Stable error classes; each has its own process exit code (documented in --help and the spec). */
export const EXIT_CODES = Object.freeze({
  internal_error: 1,
  usage: 2,
  unauthorized: 3,
  forbidden: 4,
  not_found: 5,
  conflict: 6,
  validation_failed: 7,
  rate_limited: 8,
  server_error: 9,
  network_error: 10,
  http_error: 11,
  limit_exceeded: 12,
  policy_denied: 13,
  ambiguous: 14,
  bad_response: 15,
});

export class GiteaError extends Error {
  constructor(code, message, { status = null, method = null, path = null } = {}) {
    super(message);
    this.name = "GiteaError";
    this.code = code;
    this.status = status;
    this.method = method;
    this.path = path;
  }
}

const usage = (message) => new GiteaError("usage", message);
const policy = (message) => new GiteaError("policy_denied", message);

/** Route Undici fetch calls through OneCLI's HTTPS proxy; never fall back to direct. */
export function createGatewayFetch({
  fetchImpl,
  proxy = process.env.HTTPS_PROXY ?? process.env.https_proxy,
  requestTls,
} = {}) {
  if (!proxy) throw policy("OneCLI HTTPS proxy is not configured");
  let proxyUrl;
  try {
    proxyUrl = new URL(proxy);
  } catch {
    throw policy("OneCLI HTTPS proxy URL is invalid");
  }
  if (!["http:", "https:"].includes(proxyUrl.protocol))
    throw policy("OneCLI HTTPS proxy URL must use HTTP or HTTPS");
  const { ProxyAgent, fetch: undiciFetch } = loadUndici();
  fetchImpl ??= undiciFetch;
  const dispatcher = new ProxyAgent({ uri: proxyUrl.toString(), requestTls });
  return (url, init) => {
    if (new URL(url).protocol !== "https:") throw policy("Gitea requests must use HTTPS");
    return fetchImpl(url, { ...init, dispatcher });
  };
}

/** Credential-free Gitea API client. OneCLI injects Authorization at the HTTPS gateway. */
export function allowlistedRepository(repositoryUrl, allowedRepositories) {
  const candidate = repositoryIdentity(repositoryUrl);
  const normalized = `${candidate.origin}/${candidate.owner}/${candidate.repo}`;
  const allow = new Set(
    allowedRepositories.map((raw) => {
      const url = repositoryIdentity(raw);
      return `${url.origin}/${url.owner}/${url.repo}`;
    }),
  );
  if (!allow.has(normalized)) throw policy("repository is not configured for this worker");
  return candidate;
}

function repositoryIdentity(repositoryUrl) {
  let candidate;
  try {
    candidate = new URL(repositoryUrl);
  } catch {
    throw policy("invalid repository URL");
  }
  if (
    candidate.protocol !== "https:" ||
    candidate.username ||
    candidate.password ||
    candidate.search ||
    candidate.hash ||
    candidate.pathname.endsWith("/")
  )
    throw policy("repository URL must be an HTTPS repository URL without credentials or query");
  const parts = candidate.pathname.split("/").filter(Boolean);
  if (parts.length !== 2 || !parts.every((part) => /^[A-Za-z0-9_.-]+$/.test(part)))
    throw policy("repository URL must identify exactly owner/repository");
  const owner = parts[0];
  const final = parts[1];
  const repo = final.endsWith(".git") ? final.slice(0, -4) : final;
  if (!repo || repo === "." || repo === ".." || owner === "." || owner === "..")
    throw policy("repository URL must identify exactly owner/repository");
  return { origin: candidate.origin, owner, repo };
}

// ---- input validation (every path segment comes through one of these) ----

function positiveNumber(value, name = "number") {
  const n = typeof value === "string" && /^[0-9]+$/.test(value) ? Number(value) : value;
  if (!Number.isSafeInteger(n) || n < 1) throw usage(`${name} must be a positive integer`);
  return n;
}

function boundedInt(value, name, max) {
  const n = positiveNumber(value, name);
  if (n > max) throw usage(`${name} must be 1..${max}`);
  return n;
}

const LIST_STATES = new Set(["open", "closed", "all"]);
const LIST_LIMIT_MAX = 50;

function listState(state) {
  if (typeof state !== "string" || !LIST_STATES.has(state))
    throw usage("state must be open, closed, or all");
  return state;
}

function listLimit(limit) {
  const n = Number(limit);
  if (!Number.isSafeInteger(n) || n < 1 || n > LIST_LIMIT_MAX)
    throw usage(`limit must be 1..${LIST_LIMIT_MAX}`);
  return n;
}

function commitSha(value, name = "sha") {
  if (typeof value !== "string" || !/^[0-9a-f]{7,64}$/i.test(value))
    throw usage(`${name} must be 7-64 hexadecimal characters`);
  return value.toLowerCase();
}

/** A git ref name: safe charset, no `..`, no empty segment; encoded per segment. */
function refPath(value, name = "branch") {
  if (
    typeof value !== "string" ||
    value.length > 255 ||
    !/^[A-Za-z0-9._/-]+$/.test(value) ||
    value.startsWith("-") ||
    value
      .split("/")
      .some((part) => !part || part === "." || part === ".." || part.endsWith(".lock")) ||
    value.includes("..")
  )
    throw usage(`${name} must be a plain ref name (letters, digits, . _ - /; no ..)`);
  return value.split("/").map(encodeURIComponent).join("/");
}

function plainText(value, name, max = 256) {
  if (
    typeof value !== "string" ||
    !value.length ||
    value.length > max ||
    /[\u0000-\u001f\u007f]/.test(value)
  )
    throw usage(`${name} must be 1..${max} printable characters`);
  return value;
}

function token(value, name, pattern = /^[a-z_]{1,32}$/) {
  if (typeof value !== "string" || !pattern.test(value))
    throw usage(`${name} has an invalid value`);
  return value;
}

function timestamp(value, name) {
  if (
    typeof value !== "string" ||
    !/^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2}))?$/.test(value) ||
    Number.isNaN(Date.parse(value))
  )
    throw usage(`${name} must be an ISO 8601 date or timestamp`);
  return new Date(value).toISOString();
}

function idempotencyKey(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9._:-]{1,128}$/.test(value))
    throw usage("idempotency-key must be 1..128 characters of [A-Za-z0-9._:-]");
  return value;
}

/** Defense in depth: internal API paths are relative, without traversal or encoded dots. */
function safeApiPath(path) {
  if (path === "") return path; // the repository itself
  const pathname = path.split("?")[0];
  if (
    typeof path !== "string" ||
    !path.startsWith("/") ||
    path.startsWith("//") ||
    /[\\#]|%2e|%2f|%5c/i.test(pathname) ||
    pathname.split("/").some((part) => part === "." || part === "..")
  )
    throw policy("API path must stay inside the configured repository");
  return path;
}

// ---- error sanitizing ----

const SECRETISH = [
  /\b(?:token|bearer|basic)\s+\S+/gi,
  /[a-z][a-z0-9+.-]*:\/\/[^\s/@]*@/gi,
  /[A-Za-z0-9_+-]{24,}/g,
];

/** Short, single-line message with token-like strings removed. */
export function sanitizeMessage(text, max = 200) {
  let out = String(text ?? "");
  for (const pattern of SECRETISH) out = out.replace(pattern, "[redacted]");
  out = out.replace(/\s+/g, " ").trim();
  return out.length > max ? `${out.slice(0, max)}…` : out;
}

function httpErrorCode(status) {
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 422) return "validation_failed";
  if (status === 429) return "rate_limited";
  if (status >= 500) return "server_error";
  return "http_error";
}

async function httpError(response, method, path) {
  const code = httpErrorCode(response.status);
  let detail = "";
  // 401/403 bodies come from the gateway or auth layer; never echo them.
  if (code !== "unauthorized" && code !== "forbidden") {
    try {
      const text = (await response.text()).slice(0, 4096);
      const parsed = JSON.parse(text);
      if (typeof parsed?.message === "string") detail = `: ${sanitizeMessage(parsed.message, 160)}`;
    } catch {
      // Non-JSON body: report only the status.
    }
  } else await discard(response);
  return new GiteaError(code, `Gitea API ${method} failed with HTTP ${response.status}${detail}`, {
    status: response.status,
    method,
    path,
  });
}

async function discard(response) {
  try {
    await response.body?.cancel();
  } catch {
    // Already consumed or closed.
  }
}

function networkError(error, method, path) {
  if (error instanceof GiteaError) return error;
  const name = error?.name;
  const cause = error?.cause;
  if (/redirect/i.test(`${error?.message ?? ""} ${cause?.message ?? ""}`))
    return new GiteaError("policy_denied", "redirect refused (API redirects are never followed)", {
      method,
      path,
    });
  const detail =
    name === "TimeoutError" || name === "AbortError"
      ? "request timed out"
      : typeof cause?.code === "string" && /^[A-Z_]{2,40}$/.test(cause.code)
        ? `network error (${cause.code})`
        : "network error";
  return new GiteaError("network_error", detail, { method, path });
}

const isRedirect = (status) => [301, 302, 303, 307, 308].includes(status);
const isAmbiguous = (error) =>
  error instanceof GiteaError && (error.code === "network_error" || error.code === "server_error");

function retryAfterMs(value, now) {
  if (!value) return null;
  if (/^\d+$/.test(value.trim())) return Number(value.trim()) * 1000;
  const at = Date.parse(value);
  return Number.isNaN(at) ? null : Math.max(0, at - now);
}

// ---- output tagging: number (per-repo index) vs id (global) stay explicit ----

function issueNumberFromUrl(url) {
  const match = typeof url === "string" ? /\/(?:issues|pulls)\/(\d+)$/.exec(url) : null;
  return match ? Number(match[1]) : null;
}

const ID_FIELDS = {
  issue: (i) => ({
    issue_number: i.number,
    issue_id: i.id,
    is_pull_request: Boolean(i.pull_request),
  }),
  comment: (c) => ({
    comment_id: c.id,
    issue_number: issueNumberFromUrl(c.pull_request_url) ?? issueNumberFromUrl(c.issue_url),
  }),
  review: (r) => ({ review_id: r.id }),
  review_comment: (c) => ({ review_comment_id: c.id, review_id: c.pull_request_review_id ?? null }),
  run: (r) => ({ run_id: r.id, run_number: r.run_number ?? null }),
  job: (j) => ({ job_id: j.id, run_id: j.run_id ?? null }),
  artifact: (a) => ({ artifact_id: a.id }),
  label: (l) => ({ label_id: l.id }),
  status: (s) => ({ status_id: s.id ?? null }),
  commit: () => ({}),
  file: () => ({}),
};

function tag(kind, item) {
  return item && typeof item === "object" && !Array.isArray(item)
    ? { ...ID_FIELDS[kind](item), ...item }
    : item;
}

function envelope(kind, page, extra = {}) {
  const { items, ...rest } = page;
  return { ...extra, ...rest, count: items.length, items: items.map((item) => tag(kind, item)) };
}

// ---- review findings parsing ----

const AGENTIC_REVIEW = /agentic[\s_-]*review/i;

export function isAgenticReviewComment(body) {
  return typeof body === "string" && AGENTIC_REVIEW.test(body);
}

/** Tolerant parse of an agentic-review comment: reviewed head SHA and declared finding count. */
export function parseReviewComment(body) {
  const text = typeof body === "string" ? body.replace(/[*_`]/g, "") : "";
  const head =
    /\b(?:head(?:[\s_-]*(?:sha|commit))?|reviewed[\s_-]*(?:head|commit|sha)|commit(?:[\s_-]*sha)?|sha)\b[^0-9a-f\n]{0,24}\b([0-9a-f]{7,64})\b/i.exec(
      text,
    );
  const patterns = [
    /\bfindings?[\s_-]*count\s*[:=]\s*(\d+)/i,
    /\bfindings?\s*(?::|=|\()\s*(\d+)/i,
    /\b(\d+)\s+(?:[a-z0-9-]+\s+)?findings?\b/i,
  ];
  let findingCount = null;
  for (const pattern of patterns) {
    const match = pattern.exec(text);
    if (match) {
      findingCount = Number(match[1]);
      break;
    }
  }
  if (findingCount === null && /\bno\s+(?:new\s+|open\s+)?findings\b/i.test(text)) findingCount = 0;
  return { head_sha: head ? head[1].toLowerCase() : null, finding_count: findingCount };
}

const shaMatches = (short, full) =>
  typeof short === "string" &&
  typeof full === "string" &&
  short.length >= 7 &&
  full.toLowerCase().startsWith(short.toLowerCase());

const byNewest = (a, b) =>
  Date.parse(b.updated_at ?? b.created_at ?? 0) - Date.parse(a.updated_at ?? a.created_at ?? 0) ||
  (b.id ?? 0) - (a.id ?? 0);

// ---- download safety ----

const MAX_REDIRECT_HOPS = 3;

function outputState(target) {
  let info;
  try {
    info = lstatSync(target);
  } catch (error) {
    if (error?.code === "ENOENT") return "absent";
    throw usage("--out is not accessible");
  }
  if (info.isSymbolicLink()) throw policy("--out must not be a symlink");
  if (!info.isFile() || info.size !== 0)
    throw policy("--out exists and is not an empty regular file; refusing to overwrite");
  return "empty";
}

/** Validate --out and open an exclusive temp file beside it; nothing is written to the target yet. */
export function openOutput(out, cwd = process.cwd()) {
  if (typeof out !== "string" || !out || out.includes("\0")) throw usage("--out requires a path");
  if (out.split(/[\\/]+/).includes("..")) throw policy("--out must not contain .. segments");
  const target = resolve(cwd, out);
  outputState(target);
  let parent;
  try {
    parent = statSync(dirname(target));
  } catch {
    throw usage("--out directory does not exist");
  }
  if (!parent.isDirectory()) throw usage("--out directory does not exist");
  const temp = join(dirname(target), `.${basename(target)}.${randomBytes(6).toString("hex")}.part`);
  const fd = openSync(temp, "wx", 0o600);
  return { target, temp, fd };
}

function finishOutput({ target, temp, fd }) {
  fsyncSync(fd);
  closeSync(fd);
  if (outputState(target) === "empty") return renameSync(temp, target);
  try {
    linkSync(temp, target); // fails with EEXIST instead of overwriting a racing file
    unlinkSync(temp);
  } catch (error) {
    if (error?.code === "EEXIST")
      throw policy("--out appeared while downloading; refusing to overwrite");
    outputState(target);
    renameSync(temp, target);
  }
}

function abandonOutput({ temp, fd }) {
  try {
    closeSync(fd);
  } catch {
    // Already closed.
  }
  try {
    unlinkSync(temp);
  } catch {
    // Already gone.
  }
}

/** Read a body as bytes with a hard cap; never JSON-parsed. */
async function consumeBytes(response, maxBytes, sink, method, path) {
  const limit = () =>
    new GiteaError("limit_exceeded", `response is larger than --max-bytes ${maxBytes}`, {
      status: response.status,
      method,
      path,
    });
  const declared = Number(response.headers.get("content-length"));
  if (Number.isFinite(declared) && declared > maxBytes) {
    await discard(response);
    throw limit();
  }
  const hash = createHash("sha256");
  let bytes = 0;
  if (response.body) {
    const reader = response.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > maxBytes) {
        await reader.cancel().catch(() => {});
        throw limit();
      }
      hash.update(value);
      sink(value);
    }
  }
  return { bytes, sha256: hash.digest("hex") };
}

// ---- client ----

export class GiteaApi {
  constructor(repositoryUrl, allowedRepositories, fetchImpl = createGatewayFetch(), options = {}) {
    this.repository = allowlistedRepository(repositoryUrl, allowedRepositories);
    this.fetch = fetchImpl;
    this.timeoutMs = options.timeoutMs ?? 30_000;
    this.maxRetries = options.maxRetries ?? 3;
    this.maxRetryDelayMs = options.maxRetryDelayMs ?? 10_000;
    this.sleep = options.sleep ?? ((ms) => new Promise((done) => setTimeout(done, ms)));
    this.random = options.random ?? Math.random;
    this.now = options.now ?? Date.now;
  }

  get base() {
    return `${this.repository.origin}/api/v1/repos/${encodeURIComponent(this.repository.owner)}/${encodeURIComponent(this.repository.repo)}`;
  }

  backoff(attempt, retryAfter) {
    const hinted = retryAfterMs(retryAfter, this.now());
    if (hinted !== null) return Math.min(this.maxRetryDelayMs, hinted);
    const ceiling = Math.min(this.maxRetryDelayMs, 500 * 2 ** attempt);
    return Math.round(ceiling / 2 + (this.random() * ceiling) / 2);
  }

  /** One HTTP exchange; retries only GET/HEAD on 429/5xx/network errors. Returns the unread 2xx (or allowed 3xx) Response. */
  async exchange(
    path,
    { method = "GET", body, accept = "application/json", redirect = "error", url, timeoutMs } = {},
  ) {
    const errorPath = url ? "(redirect)" : safeApiPath(path).split("?")[0];
    const target = url ?? `${this.base}${path}`;
    const idempotent = method === "GET" || method === "HEAD";
    for (let attempt = 0; ; attempt++) {
      let response;
      try {
        response = await this.fetch(target, {
          method,
          redirect,
          headers: {
            Accept: accept,
            ...(body === undefined ? {} : { "Content-Type": "application/json" }),
          },
          body: body === undefined ? undefined : JSON.stringify(body),
          signal: AbortSignal.timeout(timeoutMs ?? this.timeoutMs),
        });
      } catch (error) {
        const failure = networkError(error, method, errorPath);
        if (failure.code === "network_error" && idempotent && attempt < this.maxRetries) {
          await this.sleep(this.backoff(attempt));
          continue;
        }
        throw failure;
      }
      if (response.ok || (redirect === "manual" && isRedirect(response.status))) return response;
      if (
        idempotent &&
        attempt < this.maxRetries &&
        (response.status === 429 || response.status >= 500)
      ) {
        const hint = response.headers.get("retry-after");
        await discard(response);
        await this.sleep(this.backoff(attempt, hint));
        continue;
      }
      throw await httpError(response, method, errorPath);
    }
  }

  async request(path, { method = "GET", body } = {}) {
    const response = await this.exchange(path, { method, body });
    return parseJson(await response.text(), method, path);
  }

  /** Page through a list endpoint by page number (never by following Link URLs). */
  async paginate(path, query = {}, options = {}) {
    const { key, all = false } = options;
    const limit = listLimit(options.limit ?? 30);
    const start = positiveNumber(options.page ?? 1, "page");
    const maxItems = boundedInt(options.maxItems ?? 1000, "max-items", 10_000);
    const maxPages = boundedInt(options.maxPages ?? 50, "max-pages", 500);
    const items = [];
    const seen = new Set();
    let current = start;
    let pages = 0;
    let total = null;
    let capped = false;
    let hasNext = false;
    for (;;) {
      const qs = new URLSearchParams({ ...query, page: String(current), limit: String(limit) });
      const response = await this.exchange(`${path}?${qs}`);
      const data = parseJson(await response.text(), "GET", path);
      const batch = key ? data?.[key] : data;
      if (!Array.isArray(batch))
        throw new GiteaError("bad_response", "list response is not an array", {
          method: "GET",
          path,
        });
      const header = response.headers.get("x-total-count");
      if (header !== null && /^\d+$/.test(header)) total = Number(header);
      else if (key && Number.isSafeInteger(data.total_count)) total = data.total_count;
      pages++;
      let fresh = 0;
      for (const item of batch) {
        const id = item?.id ?? item?.sha ?? item?.filename;
        if (id !== undefined && seen.has(id)) continue;
        if (id !== undefined) seen.add(id);
        fresh++;
        if (items.length >= maxItems) {
          capped = true;
          break;
        }
        items.push(item);
      }
      const link = response.headers.get("link");
      hasNext = link
        ? /rel="?next"?/.test(link)
        : total !== null
          ? current * limit < total
          : batch.length >= limit;
      if (fresh === 0) hasNext = false; // endpoint ignores pagination
      if (!all || !hasNext || items.length >= maxItems || pages >= maxPages) break;
      current++;
    }
    const more = capped || hasNext;
    return {
      page: start,
      limit,
      pages_fetched: pages,
      total_count: total,
      has_more: more,
      // A page cut short by --max-items resumes at that same page.
      next_page: more ? (capped ? current : current + 1) : null,
      truncated: all && more,
      items,
    };
  }

  /**
   * Send a mutation once. On an ambiguous failure (network error or 5xx) run `reconcile`
   * (a readback) instead of retrying; return its result when it proves the outcome.
   */
  async mutate(path, { method, body }, reconcile) {
    try {
      return { value: await this.request(path, { method, body }), reconciled: false };
    } catch (error) {
      if (!isAmbiguous(error) || !reconcile) throw error;
      let found;
      try {
        found = await reconcile();
      } catch {
        found = undefined;
      }
      if (found !== undefined && found !== null) return { value: found, reconciled: true };
      error.message = `${error.message}; readback did not find the change (not retried)`;
      throw error;
    }
  }

  /**
   * GET a binary resource. Follows at most 3 redirects by hand, and only when the next
   * URL is HTTPS, same origin as the allowlisted repository, and has no userinfo.
   * Follow requests send Accept only (no Authorization/Cookie). Other hosts → policy_denied.
   */
  async download(path, { maxBytes, sink, timeoutMs }) {
    let response = await this.exchange(path, { accept: "*/*", redirect: "manual", timeoutMs });
    let current = `${this.base}${path}`;
    for (let hops = 0; isRedirect(response.status); hops++) {
      const location = response.headers.get("location");
      await discard(response);
      if (hops >= MAX_REDIRECT_HOPS) throw policy("too many redirects");
      let next;
      try {
        next = new URL(location ?? "", current);
      } catch {
        throw new GiteaError("bad_response", "redirect without a valid location");
      }
      if (
        next.protocol !== "https:" ||
        next.username ||
        next.password ||
        next.origin !== this.repository.origin
      )
        throw policy("redirect outside the allowlisted repository origin refused");
      current = next.toString();
      response = await this.exchange(null, {
        url: current,
        accept: "*/*",
        redirect: "manual",
        timeoutMs,
      });
    }
    const result = await consumeBytes(response, maxBytes, sink, "GET", path.split("?")[0]);
    return { ...result, content_type: response.headers.get("content-type") };
  }

  // -- pull requests (existing contract) --

  createPullRequest(input) {
    const { title, head, base, body = "" } = input;
    if (![title, head, base].every((v) => typeof v === "string" && v.length))
      throw usage("title, head, and base are required");
    return this.findOpenPullRequest(head, base).then(
      async (existing) =>
        existing ??
        (
          await this.mutate("/pulls", { method: "POST", body: { title, head, base, body } }, () =>
            this.findOpenPullRequest(head, base),
          )
        ).value,
    );
  }

  async findOpenPullRequest(head, base) {
    const branch = head.split(":").at(-1);
    const headFilter = head.includes(":") ? head : `${this.repository.owner}:${head}`;
    const query = new URLSearchParams({ state: "open", head: headFilter, base });
    const prs = await this.request(`/pulls?${query}`);
    if (!Array.isArray(prs))
      throw new GiteaError("bad_response", "Gitea pull request list is not an array");
    return (
      prs.find(
        (pr) =>
          pr.head?.ref === branch &&
          pr.base?.ref === base &&
          (!pr.head?.repo?.full_name ||
            pr.head.repo.full_name.toLowerCase() ===
              `${this.repository.owner}/${this.repository.repo}`.toLowerCase()),
      ) ?? null
    );
  }

  getPullRequest(number) {
    return this.request(`/pulls/${positiveNumber(number)}`);
  }

  /** Read-only issue tracker item via `issue-get`; `get` is pull-request-only — do not assume an issue number is a PR. */
  getIssue(number) {
    return this.request(`/issues/${positiveNumber(number)}`);
  }

  /** Read-only pull request list for the allowlisted repository. */
  listPullRequests({ state, page = 1, limit = 30 }) {
    const query = new URLSearchParams({
      state: listState(state),
      page: String(positiveNumber(page, "page")),
      limit: String(listLimit(limit)),
    });
    return this.request(`/pulls?${query}`);
  }

  updatePullRequest(number, input) {
    if (Object.keys(input).some((key) => !["title", "body", "state", "base"].includes(key)))
      throw usage("unsupported PR update field");
    const n = positiveNumber(number);
    if (!Object.keys(input).length) throw usage("nothing to update");
    // Sync validation above; async mutate+readback mirrors editIssue (base compares as ref).
    return (async () => {
      const { reconciled } = await this.mutate(
        `/pulls/${n}`,
        { method: "PATCH", body: input },
        async () => {
          const current = await this.getPullRequest(n);
          const match = Object.entries(input).every(([key, value]) => {
            if (key === "base") return (current.base?.ref ?? "") === value;
            return (current[key] ?? "") === value;
          });
          return match ? current : undefined;
        },
      );
      return { pull: await this.getPullRequest(n), reconciled };
    })();
  }

  async getPullRequestStatus(number) {
    const pr = await this.getPullRequest(number);
    const sha = pr.head?.sha;
    if (typeof sha !== "string")
      throw new GiteaError("bad_response", "PR response has no head SHA");
    // Server-supplied SHA: encoded as one segment (and path-guarded), as before.
    const statuses = await this.request(`/statuses/${encodeURIComponent(sha)}`);
    return { number: pr.number, head_sha: sha, statuses };
  }

  // -- repository --

  getRepository() {
    return this.request("");
  }

  async getBranch(name) {
    const branch = await this.request(`/branches/${refPath(name)}`);
    return {
      branch: branch?.name ?? name,
      sha: branch?.commit?.id ?? null,
      protected: branch?.protected ?? null,
      commit: branch?.commit ?? null,
    };
  }

  async getDefaultBranch() {
    const repo = await this.getRepository();
    if (typeof repo?.default_branch !== "string")
      throw new GiteaError("bad_response", "repository has no default branch");
    const branch = await this.getBranch(repo.default_branch);
    return { default_branch: repo.default_branch, sha: branch.sha };
  }

  // -- issues and labels --

  async listIssues({ state = "open", type = "issues", labels = [], q, since, before, ...page }) {
    const query = { state: listState(state), type: token(type, "type", /^(issues|pulls|all)$/) };
    if (type === "all") delete query.type;
    if (labels.length) query.labels = labels.map((l) => plainText(l, "label", 100)).join(",");
    if (q !== undefined) query.q = plainText(q, "q");
    if (since !== undefined) query.since = timestamp(since, "since");
    if (before !== undefined) query.before = timestamp(before, "before");
    return this.paginate("/issues", query, page);
  }

  async createIssue({ title, body = "", labels = [] }) {
    plainText(title, "title", 255);
    const labelIds = labels.length ? await this.labelIds(labels) : undefined;
    const started = new Date(this.now() - 10 * 60_000).toISOString();
    const { value, reconciled } = await this.mutate(
      "/issues",
      { method: "POST", body: { title, body, ...(labelIds ? { labels: labelIds } : {}) } },
      async () => {
        const recent = await this.paginate(
          "/issues",
          { state: "all", type: "issues", since: started },
          { all: true, limit: 50 },
        );
        return recent.items
          .filter((i) => i.title === title && (i.body ?? "") === body)
          .sort(byNewest)[0];
      },
    );
    return { issue: await this.getIssue(value.number), reconciled };
  }

  async editIssue(number, input) {
    const n = positiveNumber(number);
    if (input.state !== undefined) token(input.state, "state", /^(open|closed)$/);
    if (input.title !== undefined) plainText(input.title, "title", 255);
    if (!Object.keys(input).length) throw usage("nothing to update");
    const { reconciled } = await this.mutate(
      `/issues/${n}`,
      { method: "PATCH", body: input },
      async () => {
        const current = await this.getIssue(n);
        return Object.entries(input).every(([key, value]) => (current[key] ?? "") === value)
          ? current
          : undefined;
      },
    );
    return { issue: await this.getIssue(n), reconciled };
  }

  /** Repository labels by exact name → id; unknown names are refused. */
  async labelIds(names) {
    const all = await this.paginate("/labels", {}, { all: true, limit: 50 });
    return names.map((name) => {
      const found = all.items.find((label) => label.name === plainText(name, "label", 100));
      if (!found) throw usage("label is not defined in the repository");
      return found.id;
    });
  }

  listLabels(number, page) {
    return number === undefined
      ? this.paginate("/labels", {}, page)
      : this.paginate(`/issues/${positiveNumber(number)}/labels`, {}, page);
  }

  async addLabels(number, names) {
    const n = positiveNumber(number);
    const ids = await this.labelIds(names);
    const readback = async () => (await this.request(`/issues/${n}/labels`)) ?? [];
    const { reconciled } = await this.mutate(
      `/issues/${n}/labels`,
      { method: "POST", body: { labels: ids } },
      async () => {
        const current = await readback();
        return ids.every((id) => current.some((l) => l.id === id)) ? current : undefined;
      },
    );
    return { labels: await readback(), reconciled };
  }

  async removeLabels(number, names) {
    const n = positiveNumber(number);
    const ids = await this.labelIds(names);
    const readback = async () => (await this.request(`/issues/${n}/labels`)) ?? [];
    let reconciled = false;
    for (const id of ids) {
      const result = await this.mutate(
        `/issues/${n}/labels/${id}`,
        { method: "DELETE" },
        async () => {
          const current = await readback();
          return current.some((l) => l.id === id) ? undefined : current;
        },
      );
      reconciled ||= result.reconciled;
    }
    return { labels: await readback(), reconciled };
  }

  // -- comments (issues and pull requests share one API) --

  listComments(number, { since, before, ...page } = {}) {
    const query = {};
    if (since !== undefined) query.since = timestamp(since, "since");
    if (before !== undefined) query.before = timestamp(before, "before");
    return this.paginate(`/issues/${positiveNumber(number)}/comments`, query, page);
  }

  getComment(id) {
    return this.request(`/issues/comments/${positiveNumber(id, "comment-id")}`);
  }

  async findCommentByMarker(number, key) {
    const marker = commentMarker(key);
    const page = await this.listComments(number, { all: true, limit: 50, maxItems: 10_000 });
    return (
      page.items
        .filter((c) => typeof c.body === "string" && c.body.includes(marker))
        .sort(byNewest)[0] ?? null
    );
  }

  async createComment(number, body, { key } = {}) {
    const n = positiveNumber(number);
    let text = body;
    if (key !== undefined) {
      const marker = commentMarker(idempotencyKey(key));
      const existing = await this.findCommentByMarker(n, key);
      if (existing) return { comment: existing, created: false, reconciled: false };
      if (!text.includes(marker)) text = `${text}\n\n${marker}`;
    }
    const since = new Date(this.now() - 10 * 60_000).toISOString();
    const { value, reconciled } = await this.mutate(
      `/issues/${n}/comments`,
      { method: "POST", body: { body: text } },
      async () =>
        key !== undefined
          ? this.findCommentByMarker(n, key)
          : (await this.listComments(n, { since, all: true, limit: 50 })).items
              .filter((c) => c.body === text)
              .sort(byNewest)[0],
    );
    return { comment: await this.getComment(value.id), created: true, reconciled };
  }

  async editComment(id, body) {
    const commentId = positiveNumber(id, "comment-id");
    const { reconciled } = await this.mutate(
      `/issues/comments/${commentId}`,
      { method: "PATCH", body: { body } },
      async () => {
        const current = await this.getComment(commentId);
        return current?.body === body ? current : undefined;
      },
    );
    return { comment: await this.getComment(commentId), reconciled };
  }

  async deleteComment(id) {
    const commentId = positiveNumber(id, "comment-id");
    const gone = async () => {
      try {
        await this.getComment(commentId);
        return undefined;
      } catch (error) {
        if (error instanceof GiteaError && error.code === "not_found") return true;
        throw error;
      }
    };
    const { reconciled } = await this.mutate(
      `/issues/comments/${commentId}`,
      { method: "DELETE" },
      gone,
    );
    if (!(await gone()))
      throw new GiteaError("ambiguous", "comment still exists after delete", {
        method: "DELETE",
        path: "/issues/comments",
      });
    return { comment_id: commentId, deleted: true, verified: true, reconciled };
  }

  // -- pull request details and reviews --

  listPullCommits(number, page) {
    return this.paginate(`/pulls/${positiveNumber(number)}/commits`, {}, page);
  }

  listPullFiles(number, page) {
    return this.paginate(`/pulls/${positiveNumber(number)}/files`, {}, page);
  }

  listReviews(number, page) {
    return this.paginate(`/pulls/${positiveNumber(number)}/reviews`, {}, page);
  }

  getReview(number, reviewId) {
    return this.request(
      `/pulls/${positiveNumber(number)}/reviews/${positiveNumber(reviewId, "review-id")}`,
    );
  }

  async getReviewComments(number, reviewId) {
    const comments = await this.request(
      `/pulls/${positiveNumber(number)}/reviews/${positiveNumber(reviewId, "review-id")}/comments`,
    );
    return Array.isArray(comments) ? comments : [];
  }

  async createReview(number, { body = "", event = "COMMENT", commitId, comments = [] }) {
    const n = positiveNumber(number);
    if (event === "APPROVED" || event === "APPROVE")
      throw policy("approving reviews is not allowed for workers; use COMMENT or REQUEST_CHANGES");
    token(event, "event", /^(COMMENT|REQUEST_CHANGES)$/);
    const inline = reviewComments(comments);
    if (!body && !inline.length) throw usage("a review needs a body or inline comments");
    const commit_id =
      commitId === undefined
        ? (await this.getPullRequest(n)).head?.sha
        : commitSha(commitId, "commit-id");
    const payload = { body, event, commit_id, comments: inline };
    const { value, reconciled } = await this.mutate(
      `/pulls/${n}/reviews`,
      { method: "POST", body: payload },
      async () => {
        const reviews = await this.listReviews(n, { all: true, limit: 50 });
        return reviews.items
          .filter((r) => (r.body ?? "") === body && r.commit_id === commit_id)
          .sort(byNewest)[0];
      },
    );
    const review = await this.getReview(n, value.id);
    return { review, comments: await this.getReviewComments(n, value.id), reconciled };
  }

  // -- commit statuses --

  async listStatuses(sha, { state, sort, ...page } = {}) {
    const ref = encodeURIComponent(commitSha(sha));
    const query = {};
    if (state !== undefined)
      query.state = token(state, "state", /^(pending|success|error|failure|warning)$/);
    if (sort !== undefined)
      query.sort = token(
        sort,
        "sort",
        /^(oldest|recentupdate|leastupdate|leastindex|highestindex)$/,
      );
    const combined = await this.request(`/commits/${ref}/status?limit=1`);
    const list = await this.paginate(`/commits/${ref}/statuses`, query, page);
    return { sha: commitSha(sha), combined_state: combined?.state ?? null, ...list };
  }

  // -- Actions --

  listRuns({ branch, headSha, status, event, ...page } = {}) {
    const query = {};
    if (branch !== undefined)
      query.branch = decodeURIComponent(refPath(branch).replaceAll("/", "%2F"));
    if (headSha !== undefined) query.head_sha = commitSha(headSha, "head-sha");
    if (status !== undefined) query.status = token(status, "status");
    if (event !== undefined) query.event = token(event, "event");
    return this.paginate("/actions/runs", query, { ...page, key: "workflow_runs" });
  }

  getRun(id) {
    return this.request(`/actions/runs/${positiveNumber(id, "run-id")}`);
  }

  listJobs(runId, page) {
    return this.paginate(
      `/actions/runs/${positiveNumber(runId, "run-id")}/jobs`,
      {},
      { ...page, key: "jobs" },
    );
  }

  jobLogs(jobId, options) {
    return this.download(`/actions/jobs/${positiveNumber(jobId, "job-id")}/logs`, options);
  }

  listArtifacts(runId, page) {
    const path =
      runId === undefined
        ? "/actions/artifacts"
        : `/actions/runs/${positiveNumber(runId, "run-id")}/artifacts`;
    return this.paginate(path, {}, { ...page, key: "artifacts" });
  }

  getArtifact(id) {
    return this.request(`/actions/artifacts/${positiveNumber(id, "artifact-id")}`);
  }

  artifactZip(id, options) {
    return this.download(`/actions/artifacts/${positiveNumber(id, "artifact-id")}/zip`, options);
  }

  pullDiff(number, options) {
    return this.download(`/pulls/${positiveNumber(number)}.diff`, options);
  }

  // -- agentic review --

  /** Latest agentic-review comment and status for the PR's exact current head SHA. */
  async reviewFindings(number, { author } = {}) {
    const pr = await this.getPullRequest(number);
    const head = pr?.head?.sha;
    if (typeof head !== "string")
      throw new GiteaError("bad_response", "PR response has no head SHA");
    const comments = (
      await this.listComments(pr.number, { all: true, limit: 50, maxItems: 10_000 })
    ).items
      .filter(
        (c) => isAgenticReviewComment(c.body) && (author === undefined || c.user?.login === author),
      )
      .map((c) => ({ comment: c, parsed: parseReviewComment(c.body) }))
      .sort((a, b) => byNewest(a.comment, b.comment));
    const statuses = (await this.listStatuses(head, { all: true, limit: 50 })).items
      .filter((s) => AGENTIC_REVIEW.test(s.context ?? ""))
      .sort(byNewest);
    const status = statuses[0]
      ? {
          state: statuses[0].status ?? statuses[0].state ?? null,
          context: statuses[0].context ?? null,
          description: statuses[0].description ?? null,
          target_url: statuses[0].target_url ?? null,
        }
      : null;
    const exact = comments.find((c) => shaMatches(c.parsed.head_sha, head));
    const chosen = exact ?? comments[0];
    let reason = null;
    if (!chosen) reason = "no agentic-review comment found on the pull request";
    else if (!exact)
      reason = chosen.parsed.head_sha
        ? "latest agentic-review comment is for a different head SHA"
        : "agentic-review comment has no parsable head SHA";
    else if (exact.parsed.finding_count === null)
      reason = "agentic-review comment for this head has no parsable finding count";
    if (reason && status?.state === "success")
      reason += "; a green status is not evidence of zero findings";
    return {
      pr_number: pr.number,
      head_sha: head,
      comment_id: chosen?.comment.id ?? null,
      comment_head_sha: chosen?.parsed.head_sha ?? null,
      matches_head: Boolean(exact),
      finding_count: exact ? exact.parsed.finding_count : null,
      body: chosen?.comment.body ?? null,
      status,
      reason,
    };
  }
}

function parseJson(text, method, path) {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    throw new GiteaError("bad_response", "response is not JSON", {
      method,
      path: path?.split("?")[0] ?? null,
    });
  }
}

const commentMarker = (key) => `<!-- argus-key:${key} -->`;

function reviewComments(comments) {
  if (!Array.isArray(comments)) throw usage("--comments-file must hold a JSON array");
  return comments.map((c) => {
    if (!c || typeof c !== "object" || typeof c.path !== "string" || typeof c.body !== "string")
      throw usage("each inline comment needs path and body strings");
    const out = { path: plainText(c.path, "comment path", 1024), body: c.body };
    for (const key of ["new_position", "old_position"])
      if (c[key] !== undefined) out[key] = boundedInt(c[key], key, 10_000_000);
    if (out.new_position === undefined && out.old_position === undefined)
      throw usage("each inline comment needs new_position or old_position");
    return out;
  });
}

// ---- CLI ----

const BODY_FILE_MAX_BYTES = 256 * 1024;

/** `--body TEXT`, `--body-file PATH` or `--body-stdin`; at most one. Returns undefined when none is given. */
export function bodyFromOptions(options, readStdin = () => readFileSync(0)) {
  const given = ["body", "body-file", "body-stdin"].filter((key) => options[key] !== undefined);
  if (given.length > 1)
    throw usage("use either --body or --body-file or --body-stdin, not several");
  if (options["body-stdin"]) {
    const bytes = readStdin();
    if (bytes.length > BODY_FILE_MAX_BYTES)
      throw usage(`--body-stdin is larger than ${BODY_FILE_MAX_BYTES} bytes`);
    return Buffer.from(bytes).toString("utf8");
  }
  if (options["body-file"] === undefined) {
    if (options.body !== undefined && Buffer.byteLength(options.body) > BODY_FILE_MAX_BYTES)
      throw usage(`--body is larger than ${BODY_FILE_MAX_BYTES} bytes`);
    return options.body;
  }
  const file = options["body-file"];
  let info;
  try {
    info = statSync(file);
  } catch {
    throw usage("--body-file is not readable");
  }
  if (!info.isFile()) throw usage("--body-file must be a regular file");
  if (info.size > BODY_FILE_MAX_BYTES)
    throw usage(`--body-file is larger than ${BODY_FILE_MAX_BYTES} bytes`);
  return readFileSync(file, "utf8");
}

const ALIASES = { pr: "number" };
const GLOBAL_OPTIONS = {
  repository: "string",
  "timeout-ms": "int",
  format: "string",
  json: "bool",
  help: "bool",
};
const PAGE = { page: "int", limit: "int", all: "bool", "max-items": "int", "max-pages": "int" };
const BODY = { body: "string", "body-file": "string", "body-stdin": "bool" };

/** Parse `--name value` / `--name=value` (split on the first `=` only); values are kept verbatim. */
export function parseArgs(args, spec) {
  const options = {};
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (typeof arg !== "string" || !arg.startsWith("--") || arg === "--")
      throw usage("expected --name value arguments");
    const eq = arg.indexOf("=");
    const raw = eq === -1 ? arg.slice(2) : arg.slice(2, eq);
    const name = ALIASES[raw] ?? raw;
    const kind = spec[name];
    if (!kind)
      throw usage(/^[a-z0-9-]{1,40}$/.test(raw) ? `unknown option --${raw}` : "unknown option");
    let value;
    if (kind === "bool") {
      if (eq !== -1) throw usage(`--${raw} takes no value`);
      value = true;
    } else if (eq !== -1) value = arg.slice(eq + 1);
    else {
      value = args[++i];
      if (value === undefined || value === "") throw usage(`--${raw} requires a value`);
    }
    if (kind === "list") (options[name] ??= []).push(value);
    else if (name in options) throw usage(`--${raw} given more than once`);
    else options[name] = value;
  }
  return options;
}

const pageOptions = (o) => ({
  page: o.page,
  limit: o.limit,
  all: Boolean(o.all),
  maxItems: o["max-items"],
  maxPages: o["max-pages"],
});

function maxBytes(o, fallback, cap) {
  return o["max-bytes"] === undefined ? fallback : boundedInt(o["max-bytes"], "max-bytes", cap);
}

const MiB = 1024 * 1024;

/** Stream a download to --out (raw bytes) or return its bytes decoded as UTF-8 text. */
async function saveOrText(ctx, fetchBytes, limitBytes, idField) {
  const timeoutMs = ctx.options["timeout-ms"] === undefined ? 300_000 : ctx.timeoutMs;
  if (ctx.options.out === undefined) {
    const chunks = [];
    await fetchBytes({ maxBytes: limitBytes, timeoutMs, sink: (c) => chunks.push(Buffer.from(c)) });
    return { text: Buffer.concat(chunks).toString("utf8") };
  }
  const out = openOutput(ctx.options.out, ctx.cwd);
  try {
    const result = await fetchBytes({
      maxBytes: limitBytes,
      timeoutMs,
      sink: (chunk) => writeSync(out.fd, chunk),
    });
    finishOutput(out);
    return {
      ...idField,
      path: out.target,
      bytes: result.bytes,
      sha256: result.sha256,
      content_type: result.content_type,
    };
  } catch (error) {
    abandonOutput(out);
    throw error;
  }
}

function prOutput(pr) {
  return pr;
}

const COMMANDS = {
  create: {
    summary: "open a pull request (idempotent per head/base); prints the PR",
    options: { title: "string", head: "string", base: "string", ...BODY },
    run: async (api, o, ctx) => {
      const pr = await api.createPullRequest({
        title: o.title,
        head: o.head,
        base: o.base,
        body: ctx.body() ?? "",
      });
      return prOutput(await api.getPullRequest(pr.number));
    },
  },
  get: {
    summary: "pull request by number (PRs only; use issue-get for issues)",
    options: { number: "string" },
    required: ["number"],
    run: (api, o) => api.getPullRequest(o.number),
  },
  "issue-get": {
    summary: "issue tracker item by number",
    options: { number: "string" },
    required: ["number"],
    run: (api, o) => api.getIssue(o.number),
  },
  "list-prs": {
    summary: "pull requests (array output, as before); --all pages up to --max-items",
    options: { state: "string", ...PAGE },
    run: async (api, o) => {
      if (!o.state) throw usage("list-prs requires --state open|closed|all");
      if (!o.all && o["max-items"] === undefined && o["max-pages"] === undefined)
        return api.listPullRequests({
          state: o.state,
          page: o.page ?? "1",
          limit: o.limit ?? "30",
        });
      return (await api.paginate("/pulls", { state: listState(o.state) }, pageOptions(o))).items;
    },
  },
  update: {
    summary: "edit a pull request (title, body, base, state); prints the PR read back",
    options: { number: "string", title: "string", state: "string", base: "string", ...BODY },
    run: async (api, o, ctx) => {
      const body = {};
      const text = ctx.body();
      for (const key of ["title", "state", "base"]) if (o[key] !== undefined) body[key] = o[key];
      if (text !== undefined) body.body = text;
      const number = positiveNumber(o.number);
      await api.updatePullRequest(number, body);
      return prOutput(await api.getPullRequest(number));
    },
  },
  status: {
    summary: "commit statuses for the PR head SHA",
    options: { number: "string" },
    required: ["number"],
    run: (api, o) => api.getPullRequestStatus(o.number),
  },
  "repo-get": {
    summary: "the repository (includes default_branch)",
    options: {},
    run: (api) => api.getRepository(),
  },
  "default-branch": {
    summary: "{default_branch, sha}",
    options: {},
    run: (api) => api.getDefaultBranch(),
  },
  "branch-get": {
    summary: "{branch, sha, protected, commit} for a branch",
    options: { branch: "string" },
    required: ["branch"],
    run: (api, o) => api.getBranch(o.branch),
  },
  "issue-list": {
    summary: "issues (type issues|pulls|all, default issues), filters state/label/q/since/before",
    options: {
      state: "string",
      type: "string",
      label: "list",
      q: "string",
      since: "string",
      before: "string",
      ...PAGE,
    },
    run: async (api, o) =>
      envelope(
        "issue",
        await api.listIssues({
          state: o.state ?? "open",
          type: o.type ?? "issues",
          labels: o.label ?? [],
          q: o.q,
          since: o.since,
          before: o.before,
          ...pageOptions(o),
        }),
      ),
  },
  "issue-create": {
    summary: "open an issue (labels by exact name); prints {issue_number, issue_id, issue}",
    options: { title: "string", label: "list", ...BODY },
    required: ["title"],
    run: async (api, o, ctx) =>
      issueResult(
        await api.createIssue({ title: o.title, body: ctx.body() ?? "", labels: o.label ?? [] }),
      ),
  },
  "issue-update": {
    summary: "edit an issue's title/body/state; prints the issue read back",
    options: { number: "string", title: "string", state: "string", ...BODY },
    required: ["number"],
    run: async (api, o, ctx) => {
      const input = {};
      if (o.title !== undefined) input.title = o.title;
      if (o.state !== undefined) input.state = o.state;
      const text = ctx.body();
      if (text !== undefined) input.body = text;
      return issueResult(await api.editIssue(o.number, input));
    },
  },
  "issue-close": {
    summary: "close an issue; prints the issue read back",
    options: { number: "string" },
    required: ["number"],
    run: async (api, o) => issueResult(await api.editIssue(o.number, { state: "closed" })),
  },
  "issue-reopen": {
    summary: "reopen an issue; prints the issue read back",
    options: { number: "string" },
    required: ["number"],
    run: async (api, o) => issueResult(await api.editIssue(o.number, { state: "open" })),
  },
  "label-list": {
    summary: "labels of an issue/PR (--number) or of the repository",
    options: { number: "string", ...PAGE },
    run: async (api, o) =>
      envelope(
        "label",
        await api.listLabels(o.number, pageOptions(o)),
        o.number === undefined ? {} : { issue_number: positiveNumber(o.number) },
      ),
  },
  "label-add": {
    summary: "add labels (repeat --label NAME) to an issue/PR; prints labels read back",
    options: { number: "string", label: "list" },
    required: ["number", "label"],
    run: async (api, o) => labelResult(o.number, await api.addLabels(o.number, o.label)),
  },
  "label-remove": {
    summary: "remove labels (repeat --label NAME) from an issue/PR; prints labels read back",
    options: { number: "string", label: "list" },
    required: ["number", "label"],
    run: async (api, o) => labelResult(o.number, await api.removeLabels(o.number, o.label)),
  },
  "comment-list": {
    summary: "comments on an issue or PR (--number), --since/--before ISO time",
    options: { number: "string", since: "string", before: "string", ...PAGE },
    required: ["number"],
    run: async (api, o) =>
      envelope(
        "comment",
        await api.listComments(o.number, { since: o.since, before: o.before, ...pageOptions(o) }),
        {
          issue_number: positiveNumber(o.number),
        },
      ),
  },
  "comment-get": {
    summary: "one comment by global --comment-id",
    options: { "comment-id": "string" },
    required: ["comment-id"],
    run: async (api, o) => commentResult({ comment: await api.getComment(o["comment-id"]) }),
  },
  "comment-create": {
    summary: "comment on an issue or PR; --idempotency-key KEY returns an existing marked comment",
    options: { number: "string", "idempotency-key": "string", ...BODY },
    required: ["number"],
    run: async (api, o, ctx) => {
      const body = ctx.body();
      if (body === undefined)
        throw usage("comment-create requires --body, --body-file or --body-stdin");
      return commentResult(await api.createComment(o.number, body, { key: o["idempotency-key"] }));
    },
  },
  "comment-edit": {
    summary: "replace a comment's body; prints the comment read back",
    options: { "comment-id": "string", ...BODY },
    required: ["comment-id"],
    run: async (api, o, ctx) => {
      const body = ctx.body();
      if (body === undefined)
        throw usage("comment-edit requires --body, --body-file or --body-stdin");
      return commentResult(await api.editComment(o["comment-id"], body));
    },
  },
  "comment-delete": {
    summary: "delete a comment; prints {comment_id, deleted, verified} after a 404 readback",
    options: { "comment-id": "string" },
    required: ["comment-id"],
    run: (api, o) => api.deleteComment(o["comment-id"]),
  },
  "pr-commits": {
    summary: "commits of a PR",
    options: { number: "string", ...PAGE },
    required: ["number"],
    run: async (api, o) =>
      envelope("commit", await api.listPullCommits(o.number, pageOptions(o)), {
        pr_number: positiveNumber(o.number),
      }),
  },
  "pr-files": {
    summary: "changed files of a PR",
    options: { number: "string", ...PAGE },
    required: ["number"],
    run: async (api, o) =>
      envelope("file", await api.listPullFiles(o.number, pageOptions(o)), {
        pr_number: positiveNumber(o.number),
      }),
  },
  "pr-diff": {
    summary: "raw unified diff as text (default --max-bytes 5 MiB, at most 50 MiB)",
    options: { number: "string", "max-bytes": "int" },
    required: ["number"],
    raw: true,
    run: (api, o, ctx) =>
      saveOrText(ctx, (opts) => api.pullDiff(o.number, opts), maxBytes(o, 5 * MiB, 50 * MiB)),
  },
  "pr-review-list": {
    summary: "reviews of a PR (review_id, state, commit_id)",
    options: { number: "string", ...PAGE },
    required: ["number"],
    run: async (api, o) =>
      envelope("review", await api.listReviews(o.number, pageOptions(o)), {
        pr_number: positiveNumber(o.number),
      }),
  },
  "pr-review-get": {
    summary: "one review with its inline comments",
    options: { number: "string", "review-id": "string" },
    required: ["number", "review-id"],
    run: async (api, o) => ({
      pr_number: positiveNumber(o.number),
      review_id: positiveNumber(o["review-id"], "review-id"),
      review: tag("review", await api.getReview(o.number, o["review-id"])),
      comments: (await api.getReviewComments(o.number, o["review-id"])).map((c) =>
        tag("review_comment", c),
      ),
    }),
  },
  "pr-review-comments": {
    summary: "inline (thread) comments across all reviews of a PR, or one --review-id",
    options: { number: "string", "review-id": "string" },
    required: ["number"],
    run: async (api, o) => {
      const ids =
        o["review-id"] !== undefined
          ? [positiveNumber(o["review-id"], "review-id")]
          : (await api.listReviews(o.number, { all: true, limit: 50 })).items.map((r) => r.id);
      const items = [];
      for (const id of ids) items.push(...(await api.getReviewComments(o.number, id)));
      return {
        pr_number: positiveNumber(o.number),
        count: items.length,
        items: items.map((c) => tag("review_comment", c)),
      };
    },
  },
  "pr-review-create": {
    summary:
      "submit a COMMENT or REQUEST_CHANGES review (never APPROVED); --comments-file JSON inline comments",
    options: {
      number: "string",
      event: "string",
      "commit-id": "string",
      "comments-file": "string",
      ...BODY,
    },
    required: ["number"],
    run: async (api, o, ctx) => {
      let comments = [];
      if (o["comments-file"] !== undefined) {
        try {
          comments = JSON.parse(bodyFromOptions({ "body-file": o["comments-file"] }));
        } catch (error) {
          throw error instanceof GiteaError ? error : usage("--comments-file is not valid JSON");
        }
      }
      const result = await api.createReview(o.number, {
        body: ctx.body() ?? "",
        event: o.event ?? "COMMENT",
        commitId: o["commit-id"],
        comments,
      });
      return {
        pr_number: positiveNumber(o.number),
        review_id: result.review.id,
        reconciled: result.reconciled,
        review: tag("review", result.review),
        comments: result.comments.map((c) => tag("review_comment", c)),
      };
    },
  },
  statuses: {
    summary: "commit statuses for --sha with combined_state",
    options: { sha: "string", state: "string", sort: "string", ...PAGE },
    required: ["sha"],
    run: async (api, o) =>
      envelope(
        "status",
        await api.listStatuses(o.sha, { state: o.state, sort: o.sort, ...pageOptions(o) }),
      ),
  },
  "run-list": {
    summary: "Actions runs, filters --branch/--head-sha/--status/--event",
    options: { branch: "string", "head-sha": "string", status: "string", event: "string", ...PAGE },
    run: async (api, o) =>
      envelope(
        "run",
        await api.listRuns({
          branch: o.branch,
          headSha: o["head-sha"],
          status: o.status,
          event: o.event,
          ...pageOptions(o),
        }),
      ),
  },
  "run-get": {
    summary: "one Actions run by global --run-id",
    options: { "run-id": "string" },
    required: ["run-id"],
    run: async (api, o) => tag("run", await api.getRun(o["run-id"])),
  },
  "job-list": {
    summary: "jobs of an Actions run",
    options: { "run-id": "string", ...PAGE },
    required: ["run-id"],
    run: async (api, o) =>
      envelope("job", await api.listJobs(o["run-id"], pageOptions(o)), {
        run_id: positiveNumber(o["run-id"], "run-id"),
      }),
  },
  "job-logs": {
    summary:
      "job log: text on stdout, or raw bytes to a new --out file (default --max-bytes 20 MiB, at most 200 MiB)",
    options: { "job-id": "string", out: "string", "max-bytes": "int" },
    required: ["job-id"],
    raw: true,
    run: (api, o, ctx) =>
      saveOrText(ctx, (opts) => api.jobLogs(o["job-id"], opts), maxBytes(o, 20 * MiB, 200 * MiB), {
        job_id: positiveNumber(o["job-id"], "job-id"),
      }),
  },
  "artifact-list": {
    summary: "Actions artifacts of the repository or of --run-id",
    options: { "run-id": "string", ...PAGE },
    run: async (api, o) =>
      envelope("artifact", await api.listArtifacts(o["run-id"], pageOptions(o))),
  },
  "artifact-download": {
    summary:
      "save an artifact zip (not extracted) to a new --out file; prints bytes and sha256 (default --max-bytes 200 MiB, at most 2 GiB)",
    options: { "artifact-id": "string", out: "string", "max-bytes": "int" },
    required: ["artifact-id", "out"],
    run: (api, o, ctx) =>
      saveOrText(
        ctx,
        (opts) => api.artifactZip(o["artifact-id"], opts),
        maxBytes(o, 200 * MiB, 2048 * MiB),
        {
          artifact_id: positiveNumber(o["artifact-id"], "artifact-id"),
        },
      ),
  },
  "review-findings": {
    summary:
      "agentic-review comment and status for the PR's exact head SHA: {head_sha, comment_id, finding_count, body, matches_head}",
    options: { number: "string", author: "string" },
    required: ["number"],
    run: (api, o) =>
      api.reviewFindings(o.number, {
        author: o.author === undefined ? undefined : plainText(o.author, "author", 100),
      }),
  },
};

function issueResult({ issue, reconciled }) {
  return { issue_number: issue.number, issue_id: issue.id, reconciled, issue: tag("issue", issue) };
}

function labelResult(number, { labels, reconciled }) {
  return {
    issue_number: positiveNumber(number),
    reconciled,
    labels: labels.map((l) => tag("label", l)),
  };
}

function commentResult({ comment, created, reconciled }) {
  return {
    ...tag("comment", {
      id: comment.id,
      issue_url: comment.issue_url,
      pull_request_url: comment.pull_request_url,
    }),
    html_url: comment.html_url ?? null,
    ...(created === undefined ? {} : { created, reconciled }),
    comment,
  };
}

const ERROR_LINES = Object.entries(EXIT_CODES)
  .map(([code, exit]) => `  ${String(exit).padStart(2)}  ${code}`)
  .join("\n");

function usageText() {
  const width = Math.max(...Object.keys(COMMANDS).map((c) => c.length));
  const lines = Object.entries(COMMANDS).map(
    ([name, c]) => `  ${name.padEnd(width)}  ${c.summary}`,
  );
  return `usage: argus-gitea-api <command> [--repository URL] [options]

Credential-free Gitea client for the allowlisted repository (ARGUS_REPOSITORY_URL or
--repository, checked against ARGUS_REPOSITORIES_JSON). The OneCLI HTTPS proxy injects
credentials; this helper never takes a token. There is no merge, approve, admin, user,
token, secret or repository-delete command, and no arbitrary-HTTP command.

Commands:
${lines.join("\n")}

Global options: --repository URL, --timeout-ms N, --format json|text (default json), --help.
Lists: --page N, --limit N (1..50), --all [--max-items N (default 1000)] [--max-pages N (default 50)].
Bodies: --body TEXT | --body-file PATH | --body-stdin (at most 256 KiB, kept verbatim).
Options accept --name value or --name=value. number = per-repository issue/PR index;
*_id = global ids (comment, review, run, job, artifact). --pr is an alias of --number.

Errors are one JSON line on stderr: {"error":{"code","status","method","path","message"}}.
Exit codes:
   0  ok
${ERROR_LINES}
Run argus-gitea-api <command> --help for its options.
`;
}

function commandHelp(name) {
  const c = COMMANDS[name];
  const required = new Set(c.required ?? []);
  const flags = Object.entries(c.options).map(([flag, kind]) => {
    const text =
      kind === "bool" ? `--${flag}` : kind === "list" ? `--${flag} VALUE...` : `--${flag} VALUE`;
    return required.has(flag) ? text : `[${text}]`;
  });
  return `usage: argus-gitea-api ${name} ${flags.join(" ")}\n\n${c.summary}\n`;
}

const TEXT_KEYS = [
  "issue_number",
  "comment_id",
  "review_id",
  "review_comment_id",
  "run_id",
  "job_id",
  "artifact_id",
  "label_id",
  "number",
  "sha",
  "filename",
  "state",
  "status",
  "conclusion",
  "context",
  "name",
  "title",
  "path",
];

function toText(result) {
  const scalar = (v) => v === null || ["string", "number", "boolean"].includes(typeof v);
  const line = (item) =>
    TEXT_KEYS.filter((k) => item && scalar(item[k]) && item[k] !== null)
      .map((k) => `${k}=${String(item[k]).replace(/\s+/g, " ").slice(0, 120)}`)
      .join("\t");
  if (Array.isArray(result)) return result.map(line).join("\n");
  if (result && Array.isArray(result.items)) return result.items.map(line).join("\n");
  if (result && typeof result === "object")
    return Object.entries(result)
      .filter(([, v]) => scalar(v))
      .map(([k, v]) => `${k}: ${v}`)
      .join("\n");
  return String(result);
}

/** Error JSON for stderr: code/status/method/path and a sanitized message only. */
export function errorPayload(error) {
  if (error instanceof GiteaError)
    return {
      code: error.code,
      status: error.status,
      method: error.method,
      path: error.path,
      message: sanitizeMessage(error.message),
    };
  return {
    code: "internal_error",
    status: null,
    method: null,
    path: null,
    message: "internal error",
  };
}

export function exitCodeFor(error) {
  return error instanceof GiteaError ? (EXIT_CODES[error.code] ?? 1) : 1;
}

function allowlistFrom(env) {
  let parsed;
  try {
    parsed = JSON.parse(env.ARGUS_REPOSITORIES_JSON ?? "{}");
  } catch {
    throw policy("ARGUS_REPOSITORIES_JSON is not valid JSON");
  }
  const values = parsed && typeof parsed === "object" ? Object.values(parsed) : [];
  return values.filter((v) => typeof v === "string");
}

/** CLI entry point; injectable for tests. Returns the exit code. */
export async function main(argv, env = process.env, io = {}) {
  const stdout = io.stdout ?? process.stdout;
  const stderr = io.stderr ?? process.stderr;
  try {
    const [command, ...rest] = argv;
    if (!command || ["help", "--help", "-h"].includes(command)) {
      stdout.write(usageText());
      return 0;
    }
    const definition = Object.hasOwn(COMMANDS, command) ? COMMANDS[command] : null;
    if (!definition) throw usage("unknown command; run argus-gitea-api --help");
    const options = parseArgs(rest, { ...GLOBAL_OPTIONS, ...definition.options });
    if (options.help) {
      stdout.write(commandHelp(command));
      return 0;
    }
    const format = options.json ? "json" : (options.format ?? "json");
    token(format, "format", /^(json|text)$/);
    for (const flag of definition.required ?? [])
      if (options[flag] === undefined) throw usage(`${command} requires --${flag}`);
    const timeoutMs =
      options["timeout-ms"] === undefined
        ? 30_000
        : boundedInt(options["timeout-ms"], "timeout-ms", 3_600_000);
    const repository = options.repository ?? env.ARGUS_REPOSITORY_URL;
    if (!repository)
      throw policy("no repository: pass --repository URL or set ARGUS_REPOSITORY_URL");
    const allowed = allowlistFrom(env);
    allowlistedRepository(repository, allowed);
    const fetchImpl =
      io.fetch ?? createGatewayFetch({ proxy: env.HTTPS_PROXY ?? env.https_proxy ?? "" });
    const api = new GiteaApi(repository, allowed, fetchImpl, {
      timeoutMs,
      sleep: io.sleep,
      random: io.random,
      now: io.now,
    });
    const ctx = {
      options,
      timeoutMs,
      cwd: io.cwd ?? process.cwd(),
      body: () => bodyFromOptions(options, io.readStdin),
    };
    const result = await definition.run(api, options, ctx);
    if (result && typeof result.text === "string" && definition.raw) stdout.write(result.text);
    else stdout.write(`${format === "text" ? toText(result) : JSON.stringify(result)}\n`);
    return 0;
  } catch (error) {
    stderr.write(`${JSON.stringify({ error: errorPayload(error) })}\n`);
    return exitCodeFor(error);
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main(process.argv.slice(2)).then((code) => {
    process.exitCode = code;
  });
}
