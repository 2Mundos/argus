#!/usr/bin/env node
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
let ProxyAgent;
let undiciFetch;
try {
  ({ ProxyAgent, fetch: undiciFetch } = require("undici"));
} catch {
  // Installed into a dedicated, pinned runtime location in the agent image.
  ({ ProxyAgent, fetch: undiciFetch } = createRequire("/usr/local/lib/argus-gitea/package.json")(
    "undici",
  ));
}

/** Route Undici fetch calls through OneCLI's HTTPS proxy; never fall back to direct. */
export function createGatewayFetch({
  fetchImpl = undiciFetch,
  proxy = process.env.HTTPS_PROXY ?? process.env.https_proxy,
  requestTls,
} = {}) {
  if (!proxy) throw new Error("OneCLI HTTPS proxy is not configured");
  const proxyUrl = new URL(proxy);
  if (!["http:", "https:"].includes(proxyUrl.protocol))
    throw new Error("OneCLI HTTPS proxy URL must use HTTP or HTTPS");
  const dispatcher = new ProxyAgent({ uri: proxyUrl.toString(), requestTls });
  return (url, init) => {
    if (new URL(url).protocol !== "https:") throw new Error("Gitea requests must use HTTPS");
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
  if (!allow.has(normalized)) throw new Error("repository is not configured for this worker");
  return candidate;
}

function repositoryIdentity(repositoryUrl) {
  let candidate;
  try {
    candidate = new URL(repositoryUrl);
  } catch {
    throw new Error("invalid repository URL");
  }
  if (
    candidate.protocol !== "https:" ||
    candidate.username ||
    candidate.password ||
    candidate.search ||
    candidate.hash ||
    candidate.pathname.endsWith("/")
  )
    throw new Error("repository URL must be an HTTPS repository URL without credentials or query");
  const parts = candidate.pathname.split("/").filter(Boolean);
  if (parts.length !== 2 || !parts.every((part) => /^[A-Za-z0-9_.-]+$/.test(part)))
    throw new Error("repository URL must identify exactly owner/repository");
  const owner = parts[0];
  const final = parts[1];
  const repo = final.endsWith(".git") ? final.slice(0, -4) : final;
  if (!repo) throw new Error("repository name must not be empty");
  return { origin: candidate.origin, owner, repo };
}

export class GiteaApi {
  constructor(repositoryUrl, allowedRepositories, fetchImpl = createGatewayFetch()) {
    this.repository = allowlistedRepository(repositoryUrl, allowedRepositories);
    this.fetch = fetchImpl;
  }

  async request(path, { method = "GET", body } = {}) {
    const url = `${this.repository.origin}/api/v1/repos/${encodeURIComponent(this.repository.owner)}/${encodeURIComponent(this.repository.repo)}${path}`;
    const response = await this.fetch(url, {
      method,
      redirect: "error",
      headers: {
        Accept: "application/json",
        ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    if (!response.ok) throw new Error(`Gitea API ${method} failed with HTTP ${response.status}`);
    return text ? JSON.parse(text) : null;
  }

  createPullRequest(input) {
    const { title, head, base, body = "" } = input;
    if (![title, head, base].every((v) => typeof v === "string" && v.length))
      throw new Error("title, head, and base are required");
    return this.findOpenPullRequest(head, base).then(
      (existing) =>
        existing ?? this.request("/pulls", { method: "POST", body: { title, head, base, body } }),
    );
  }

  async findOpenPullRequest(head, base) {
    const branch = head.split(":").at(-1);
    const headFilter = head.includes(":") ? head : `${this.repository.owner}:${head}`;
    const query = new URLSearchParams({ state: "open", head: headFilter, base });
    const prs = await this.request(`/pulls?${query}`);
    if (!Array.isArray(prs)) throw new Error("Gitea pull request list is not an array");
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

  updatePullRequest(number, input) {
    if (Object.keys(input).some((key) => !["title", "body", "state", "base"].includes(key)))
      throw new Error("unsupported PR update field");
    return this.request(`/pulls/${positiveNumber(number)}`, { method: "PATCH", body: input });
  }

  async getPullRequestStatus(number) {
    const pr = await this.getPullRequest(number);
    const sha = pr.head?.sha;
    if (typeof sha !== "string") throw new Error("PR response has no head SHA");
    const statuses = await this.request(`/statuses/${encodeURIComponent(sha)}`);
    return { number: pr.number, head_sha: sha, statuses };
  }
}

function positiveNumber(value) {
  const n = Number(value);
  if (!Number.isSafeInteger(n) || n < 1) throw new Error("PR number must be a positive integer");
  return n;
}

async function main(argv) {
  const [command, ...args] = argv;
  const options = {};
  for (let i = 0; i < args.length; i++) {
    const key = args[i];
    if (!key.startsWith("--") || !args[i + 1]) throw new Error("expected --name value arguments");
    options[key.slice(2)] = args[++i];
  }
  const allowed = JSON.parse(process.env.ARGUS_REPOSITORIES_JSON ?? "{}");
  const repository = options.repository ?? process.env.ARGUS_REPOSITORY_URL;
  const api = new GiteaApi(repository, Object.values(allowed));
  let result;
  if (command === "create") {
    result = await api.createPullRequest({
      title: options.title,
      head: options.head,
      base: options.base,
      body: options.body ?? "",
    });
  } else if (command === "get") {
    result = await api.getPullRequest(options.number);
  } else if (command === "update") {
    const body = {};
    for (const key of ["title", "body", "state", "base"])
      if (options[key] !== undefined) body[key] = options[key];
    result = await api.updatePullRequest(options.number, body);
  } else if (command === "status") {
    result = await api.getPullRequestStatus(options.number);
  } else {
    throw new Error(
      "usage: gitea-api.mjs <create|get|update|status> --repository URL [--number N]",
    );
  }
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main(process.argv.slice(2)).catch((error) => {
    process.stderr.write(
      `${error instanceof Error ? error.message : "Gitea API request failed"}\n`,
    );
    process.exitCode = 1;
  });
}
