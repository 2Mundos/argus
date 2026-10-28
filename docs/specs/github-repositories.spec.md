# GitHub repositories

# 1. Purpose and Scope

Repository access is Gitea-only: the `repository` account role accepts only the
`gitea` provider, the NanoClaw host injects Gitea-shaped credentials, workers have
only `argus-gitea-api`, and Argus reads PR state from Gitea's REST v1. Argus itself now
lives on GitHub (`2Mundos/argus`), so a worker on an Argus task cannot open a pull
request. This spec adds GitHub as a second repository provider with the same
model: an account in the registry, a task-scoped lease, credentials injected by the
OneCLI proxy and never seen by the worker, and a credential-free helper,
`argus-github-api`, with the full command matrix of the [Gitea
helper](gitea-helper.spec.md).

In scope: the `github` account provider (dashboard, API, lease), provider choice per
repository, OneCLI injection for github.com, `argus-github-api`, the task prompt, the
host-side preflight and continuation observer, the `argus-delegate` CI probe, and
delivery PR-state reads ([task delivery](task-delivery.spec.md)).

# 2. Non-Goals

- No GitHub Enterprise Server. Only `https://github.com/{owner}/{repo}`; any other host
  stays Gitea.
- No GitHub App. The credential is a fine-grained personal access token, rotated by
  hand like the Gitea PAT; minting hourly installation tokens is out of scope.
- No `gh` CLI in agent images: it needs a token in its environment and reaches any
  endpoint.
- No change to the Gitea path: `argus-gitea-api` keeps its commands, output and exit
  codes; Gitea accounts, leases and injection are unchanged.
- No merge, approve, admin, token, secret, webhook or repository-settings command, as for
  Gitea.

# 3. Decisions Made

## 3.1 The provider follows the repository host

A repository on host `github.com` is a `github` repository; every other host is
`gitea`. The rule lives in one function per language (`repository_provider` in
`argus_core.accounts`, `repositoryProvider` in `github-api.mjs`) and nothing else
decides it. Core accepts both providers for the `repository` role and neither as a
model account (`REPOSITORY_PROVIDERS`). The NanoClaw host asks for a repository lease
with `runtime_providers` set to the one provider of the task's repository, so a
GitHub task never receives a Gitea token and the reverse.

## 3.2 Account and credential

Provider `github`, kind `api_key`, credential `{"token": "<fine-grained PAT>"}`. The
token is required, single-line, and stored in SOPS like every credential; there is no
username (Git authenticates with a fixed one, 3.3). Rotation keeps the same shape. The
operator creates the token for the bot or service identity, limited to the
allowlisted repositories, with repository permissions: Contents read/write, Pull
requests read/write, Issues read/write, Commit statuses read, Checks read, Actions
read, Metadata read. The dashboard's Add and Replace dialogs show one token field for
`github`.

## 3.3 Injection

The host writes the same two OneCLI `generic` secrets as for Gitea, with GitHub's
hosts and formats:

| Secret | Host | Path | Header |
| --- | --- | --- | --- |
| `…-repository-rest` | `api.github.com` | `/repos/{owner}/{repo}/*` | `Authorization: Bearer <token>` |
| `…-repository-git` | `github.com` | `/{owner}/{repo}.git/*` | `Authorization: Basic base64(x-access-token:<token>)` |

The REST host differs from the clone host, so the REST rule cannot match a Git path and
the reverse. Everything else (allowlist check before applying, reconcile of the full
lease set, unassign on release, no token in the task, env, prompt or logs) is the Gitea
behaviour of [worker lifecycle](worker-lifecycle.spec.md).

## 3.4 `argus-github-api`

A separate binary with the CLI of `argus-gitea-api`: the same commands, options,
envelopes, error JSON, exit codes, retry and readback rules, allowlist
(`ARGUS_REPOSITORIES_JSON`), proxy-only transport and `--out` safety. It refuses a
repository whose host is not `github.com` (`policy_denied`). It is `github-api.mjs`, a
`GitHubApi` subclass of `GiteaApi` that overrides the endpoint layer and imports the
shared plumbing from `gitea-api.mjs`; both files are installed side by side.

Where GitHub differs, the helper adapts and the difference is visible in `--help`:

| Area | GitHub behaviour |
| --- | --- |
| Base | `https://api.github.com/repos/{owner}/{repo}`, header `X-GitHub-Api-Version: 2022-11-28`. |
| Lists | `per_page` instead of `limit` (still 1..50). |
| CI | `status` and `statuses` add the head SHA's **check runs** (GitHub Actions report there, not as statuses) and a `combined_state` over both: `failure` if any status is `failure`/`error` or a completed check run concluded `failure`, `timed_out`, `cancelled`, `action_required` or `startup_failure`; `pending` if any status is `pending` or a check run is not completed; `success` otherwise; `null` with neither. |
| Issues | `issue-list --type` filters client-side on `pull_request` (pages can come back short). `--q` and `--before` are refused (`usage`): the search API is outside the repository path the proxy authorizes. |
| Comments | `comment-list --before` is refused (`usage`). |
| Statuses | `statuses --state/--sort` are refused (`usage`). |
| Labels | Sent by name, still validated against the repository's labels. Removal replaces the issue's label set (`PUT`), so no label name is ever a path segment. |
| Reviews | Inline comments keep the Gitea input (`path`, `body`, `new_position` or `old_position`) and are sent as `line` with `side` `RIGHT` or `LEFT`. |
| Diff | `GET /pulls/{n}` with `Accept: application/vnd.github.diff`. |
| Downloads | Job logs and artifact zips redirect to signed storage. A hop is followed only to HTTPS, no userinfo, a query string present, and a host under `actions.githubusercontent.com` or `blob.core.windows.net`; it sends `Accept` only. Anything else is `policy_denied`. |
| Review gate | `review-findings` also considers check runs whose name mentions the agentic review. |
| Rate limits | A 403 with `x-ratelimit-remaining: 0` is `rate_limited` (exit 8). |

Authorized merges use `PUT /pulls/{n}/merge` with the head `sha` through
`GitHubApi.request`, under the same task rules as Gitea.

## 3.5 Callers pick the client by host

`github-api.mjs` exports `repositoryClient(url, allowlist, fetch?)`, which returns a
`GitHubApi` or a `GiteaApi` per 3.1. The continuation observer and the
`argus-delegate` CI probe use it. The host preflight does not: the host has no OneCLI
proxy, so it runs `argus-github-api repo-preflight` (or the Gitea helper, by host) in
an ephemeral session container ([failure classes](failure-classes.spec.md) 3.3.1). Both clients expose `ciStatuses(sha)`: Gitea
returns its statuses as before; GitHub returns statuses plus check runs mapped to
`{id, context, state}`, so `milestones.mjs` reduces either without knowing the forge.

The task prompt names the helper of the task's provider (`argus-github-api` or
`argus-gitea-api`) and, for merge-authorized GitHub tasks, the GitHub merge call.

## 3.6 Delivery reads

`PrChecker` reads a GitHub PR from `https://api.github.com/repos/{owner}/{repo}/pulls/{n}`
with `Authorization: Bearer` and the highest-priority enabled `github` account; Gitea
PRs keep Basic auth and the `gitea` account. A GitHub PR URL is
`https://github.com/{owner}/{repo}/pull/{n}`. The rest of [task
delivery](task-delivery.spec.md) (allowlist, timeout, coalescing, `unknown` on failure)
holds.

# 4. Open Questions

1. The OneCLI egress proxy must reach the signed storage hosts of 3.4 for `job-logs` and
   `artifact-download`; verify on the first canary.
2. Fine-grained PAT permission names change over time; confirm the list in 3.2 against
   GitHub's settings page when the first token is created.

# 5. Constraints

- `github-api.mjs` depends on `gitea-api.mjs` and `undici` only, Node 22. Every install
  path that places `argus-gitea-api` also places `gitea-api.mjs`, `github-api.mjs` and
  `argus-github-api` in `/usr/local/bin`.
- `gitea-api.mjs` changes are hooks only (product name, header and paging hooks, a
  redirect policy method, the `main` options); its output for Gitea is unchanged and its
  tests stay green unmodified.
- Tests are offline: `github-api.test.mjs` with a mocked fetch, the installer pytest
  for both binaries, API tests for validation, leasing and delivery reads.

# 6. Implementation Consequences

- `packages/core`: `REPOSITORY_PROVIDERS`, `repository_provider`, `role_accepts`.
- `apps/api`: `github` credential validation and rotation, `PrChecker` per provider,
  cost report excludes both repository providers.
- `apps/dashboard`: `github` provider option and token-only form in Add and Replace.
- `integrations/nanoclaw`: `github-api.mjs` (+ `.d.mts`, tests),
  `normalizeRepositoryUrl` (provider, REST host and path), `applyRepositoryLease`,
  repository lease request, preflight, observer, prompt, `argus-delegate` probe, install
  script, tools layer, engines patcher, Radar layer, CI tools-layer smoke.
- Rollout is the Gitea helper's: rebuild the agent image or tools layer, canary
  `argus-github-api --help`, `repo-get`, `status --pr N` on one worker, then recreate the
  rest. Register the `github` account and point the `argus` project's repository at
  `https://github.com/2Mundos/argus` in the pool config.
