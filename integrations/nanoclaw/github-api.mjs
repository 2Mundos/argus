#!/usr/bin/env node
// Credential-free GitHub helper for workers (argus-github-api). Spec: docs/specs/github-repositories.spec.md.
// The CLI, errors, retries, readback and download safety are argus-gitea-api's; this
// file only adapts the endpoint layer to the GitHub REST API.
import {
  createGatewayFetch,
  main as forgeMain,
  GiteaApi,
  GiteaError,
  internals,
} from "./gitea-api.mjs";

const {
  usage,
  policy,
  positiveNumber,
  commitSha,
  listState,
  listLimit,
  plainText,
  token,
  timestamp,
} = internals;

const GITHUB_ORIGIN = "https://github.com";
const API = "https://api.github.com";
// Completed check-run conclusions that block; success, neutral and skipped do not, and any
// other conclusion (stale, null, new values) is unknown.
const FAILED_CONCLUSIONS = new Set([
  "failure",
  "timed_out",
  "cancelled",
  "action_required",
  "startup_failure",
]);
// Signed storage that job logs and artifact zips redirect to.
const STORAGE_HOSTS = [".actions.githubusercontent.com", ".blob.core.windows.net"];

/** `github` for a github.com repository URL, `gitea` for any other host. */
export function repositoryProvider(repositoryUrl) {
  try {
    return new URL(repositoryUrl).origin === GITHUB_ORIGIN ? "github" : "gitea";
  } catch {
    return "gitea";
  }
}

/** The forge client for a repository: GitHubApi on github.com, GiteaApi elsewhere. */
export function repositoryClient(repositoryUrl, allowedRepositories, fetchImpl, options) {
  const Api = repositoryProvider(repositoryUrl) === "github" ? GitHubApi : GiteaApi;
  return new Api(repositoryUrl, allowedRepositories, fetchImpl ?? createGatewayFetch(), options);
}

const PENDING_STATES = new Set(["pending", "queued", "in_progress", "waiting", "requested"]);

/** A commit status state as failure, pending, success, neutral, skipped or unknown. */
function statusState(state) {
  if (state === "failure" || state === "error" || state === "cancelled" || state === "timed_out")
    return "failure";
  if (PENDING_STATES.has(state)) return "pending";
  return ["success", "neutral", "skipped"].includes(state) ? state : "unknown";
}

/** A check run as failure, pending, success, neutral, skipped or unknown (stale, null, new). */
function checkState(run) {
  if (run?.status === "completed") {
    if (FAILED_CONCLUSIONS.has(run.conclusion)) return "failure";
    return ["success", "neutral", "skipped"].includes(run.conclusion) ? run.conclusion : "unknown";
  }
  return PENDING_STATES.has(run?.status) ? "pending" : "unknown";
}

/** One state over the latest status per context and the latest check run per name. */
function combinedState(statuses, checkRuns) {
  const states = [...statuses.map((s) => statusState(s.state)), ...checkRuns.map(checkState)];
  if (!states.length) return null;
  for (const state of ["failure", "pending", "unknown"]) {
    if (states.includes(state)) return state;
  }
  return states.some((s) => s === "success" || s === "neutral") ? "success" : "skipped";
}

export class GitHubApi extends GiteaApi {
  constructor(repositoryUrl, allowedRepositories, fetchImpl, options) {
    super(repositoryUrl, allowedRepositories, fetchImpl, options);
    if (this.repository.origin !== GITHUB_ORIGIN)
      throw policy("argus-github-api serves github.com repositories; use argus-gitea-api");
  }

  get product() {
    return "GitHub";
  }

  get pageParam() {
    return "per_page";
  }

  get headers() {
    return { "X-GitHub-Api-Version": "2022-11-28" };
  }

  get base() {
    return `${API}/repos/${encodeURIComponent(this.repository.owner)}/${encodeURIComponent(this.repository.repo)}`;
  }

  /** Logs and artifacts redirect to signed, short-lived storage URLs; nothing else is followed. */
  redirectAllowed(next) {
    return (
      next.protocol === "https:" &&
      !next.username &&
      !next.password &&
      next.search.length > 1 &&
      STORAGE_HOSTS.some((suffix) => next.hostname.endsWith(suffix))
    );
  }

  labelKey(label) {
    return label.name;
  }

  /**
   * GitHub's own spelling of the repository, as text the host verifies before any use
   * (GitHub repositories spec 3.2). The helper itself never acts on it.
   */
  canonicalRepository(data) {
    const pick = (value) => (typeof value === "string" && value.length <= 300 ? value : null);
    return { full_name: pick(data?.full_name), clone_url: pick(data?.clone_url) };
  }

  inlineComment({ path, body, new_position, old_position }) {
    return new_position === undefined
      ? { path, body, line: old_position, side: "LEFT" }
      : { path, body, line: new_position, side: "RIGHT" };
  }

  listPullRequests({ state, page = 1, limit = 30 }) {
    const query = new URLSearchParams({
      state: listState(state),
      page: String(positiveNumber(page, "page")),
      per_page: String(listLimit(limit)),
    });
    return this.request(`/pulls?${query}`);
  }

  // -- CI: commit statuses plus check runs (GitHub Actions reports only the latter) --

  async checkRuns(sha) {
    const ref = encodeURIComponent(sha);
    return (
      await this.paginate(
        `/commits/${ref}/check-runs`,
        {},
        { all: true, limit: 50, key: "check_runs" },
      )
    ).items;
  }

  /** The latest status per context (GitHub's combined status). */
  async latestStatuses(sha) {
    const combined = await this.request(`/commits/${encodeURIComponent(sha)}/status?per_page=100`);
    return Array.isArray(combined?.statuses) ? combined.statuses : [];
  }

  async getPullRequestStatus(number) {
    const pr = await this.getPullRequest(number);
    const sha = pr.head?.sha;
    if (typeof sha !== "string")
      throw new GiteaError("bad_response", "PR response has no head SHA");
    const statuses = await this.latestStatuses(sha);
    const check_runs = await this.checkRuns(sha);
    return {
      number: pr.number,
      head_sha: sha,
      combined_state: combinedState(statuses, check_runs),
      statuses,
      check_runs,
    };
  }

  async listStatuses(sha, { state, sort, ...page } = {}) {
    if (state !== undefined || sort !== undefined)
      throw usage("--state and --sort are not supported on GitHub");
    const ref = commitSha(sha);
    const list = await this.paginate(`/commits/${encodeURIComponent(ref)}/statuses`, {}, page);
    const check_runs = await this.checkRuns(ref);
    const combined = combinedState(await this.latestStatuses(ref), check_runs);
    return { sha: ref, combined_state: combined, check_runs, ...list };
  }

  async ciStatuses(sha) {
    const runs = (await this.checkRuns(sha)).map((r) => ({
      id: r.id,
      context: r.name,
      state: checkState(r),
    }));
    const statuses = (await this.latestStatuses(sha)).map((s) => ({
      ...s,
      state: statusState(s.state),
    }));
    return [...statuses, ...runs];
  }

  async reviewSignals(sha) {
    const runs = (await this.checkRuns(sha)).map((r) => ({
      id: r.id,
      context: r.name,
      state: checkState(r),
      description: r.output?.title ?? null,
      target_url: r.html_url ?? null,
      updated_at: r.completed_at ?? r.started_at ?? null,
    }));
    return [...(await this.latestStatuses(sha)), ...runs];
  }

  // -- issues, labels, comments --

  async listIssues({ state = "open", type = "issues", labels = [], q, since, before, ...page }) {
    if (q !== undefined || before !== undefined)
      throw usage("--q and --before are not supported on GitHub");
    token(type, "type", /^(issues|pulls|all)$/);
    const query = { state: listState(state) };
    if (labels.length) query.labels = labels.map((l) => plainText(l, "label", 100)).join(",");
    if (since !== undefined) query.since = timestamp(since, "since");
    const result = await this.paginate("/issues", query, page);
    if (type === "all") return result;
    // GitHub lists PRs as issues; filtering here can leave a page short.
    return {
      ...result,
      items: result.items.filter((i) => Boolean(i.pull_request) === (type === "pulls")),
    };
  }

  /** Labels are sent by name; names are still checked against the repository. */
  async labelIds(names) {
    const all = await this.paginate("/labels", {}, { all: true, limit: 50 });
    return names.map((name) => {
      if (!all.items.some((label) => label.name === plainText(name, "label", 100)))
        throw usage("label is not defined in the repository");
      return name;
    });
  }

  /** Replace the label set rather than DELETE by name: a name never becomes a path segment. */
  async removeLabels(number, names) {
    const n = positiveNumber(number);
    const drop = new Set(await this.labelIds(names));
    const readback = async () => (await this.request(`/issues/${n}/labels`)) ?? [];
    const keep = (await readback()).map((l) => l.name).filter((name) => !drop.has(name));
    const { reconciled } = await this.mutate(
      `/issues/${n}/labels`,
      { method: "PUT", body: { labels: keep } },
      async () => {
        const current = await readback();
        return current.some((l) => drop.has(l.name)) ? undefined : current;
      },
    );
    return { labels: await readback(), reconciled };
  }

  listComments(number, { since, before, ...page } = {}) {
    if (before !== undefined) throw usage("--before is not supported on GitHub");
    return super.listComments(number, { since, ...page });
  }

  /** Merge at the expected head SHA; returns the PR read back, merged with a merge commit. */
  async mergePullRequest(number, { sha, merge_method = "merge" }) {
    const n = positiveNumber(number);
    const expected = commitSha(sha);
    const method = token(merge_method, "merge-method", /^(merge|squash|rebase)$/);
    const pr = await this.getPullRequest(n);
    if (pr.state !== "open" || pr.merged === true)
      throw new GiteaError("conflict", "pull request is not open or is already merged");
    if (pr.head?.sha !== expected)
      throw new GiteaError(
        "conflict",
        "pull request head moved since the expected SHA; re-run the gates on the new head",
      );
    const merged = (current) =>
      current?.merged === true && current.merge_commit_sha ? current : undefined;
    await this.mutate(
      `/pulls/${n}/merge`,
      { method: "PUT", body: { sha: expected, merge_method: method } },
      async () => merged(await this.getPullRequest(n)),
    );
    const readback = await this.getPullRequest(n);
    if (!merged(readback))
      throw new GiteaError("bad_response", "merge readback shows no merged PR with a merge commit");
    return readback;
  }

  pullDiff(number, options) {
    return this.download(`/pulls/${positiveNumber(number)}`, {
      ...options,
      accept: "application/vnd.github.diff",
    });
  }
}

const NOTES = `GitHub differences: status and statuses add the head SHA's check runs (GitHub
Actions) and combine them into combined_state; issue-list --type filters client-side
(pages can come back short); --q, --before, and statuses --state/--sort are refused.
merge: refused unless the Argus channel issued an authorization for this attempt (pr_merged
tasks with auto_merge or allow_merge, with repository:write): ARGUS_MERGE_AUTH_KEY, _TASK and
_ATTEMPT plus ARGUS_MERGE_AUTHORIZED, the HMAC-SHA256 of the key over merge:TASK:ATTEMPT. A
missing token, a bare or worker-set one, and a stale or altered one are refused; needs --sha
(the expected head SHA), --merge-method merge|squash|rebase (default merge); sends PUT /pulls/N/merge and prints the PR read back.`;

/** CLI entry point; injectable for tests. Returns the exit code. */
export function main(argv, env = process.env, io = {}) {
  return forgeMain(argv, env, io, {
    Api: GitHubApi,
    name: "argus-github-api",
    product: "GitHub",
    notes: NOTES,
  });
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main(process.argv.slice(2)).then((code) => {
    process.exitCode = code;
  });
}
