# Gitea helper for workers (`argus-gitea-api`)

# 1. Purpose and Scope

Workers deliver through Gitea: they open a pull request, read its CI and review results,
answer review findings, and file follow-up issues. `argus-gitea-api` is the credential-free
client they use for this ([worker lifecycle](worker-lifecycle.spec.md) 3.8). It was
limited to six pull-request commands, so a worker could not list issues, read Actions
logs, post a comment, or tell which agentic review covers the current head SHA. This
spec widens the command set to cover the whole delivery cycle. The auth model, the
repository allowlist, and the six existing commands and their output stay as they are.

The helper is one self-contained file, `integrations/nanoclaw/gitea-api.mjs` (types in
`gitea-api.d.mts`), installed in agent images as `/usr/local/bin/argus-gitea-api`. Its
only dependency is the pinned `undici`.

# 2. Non-Goals

- No merge command. Authorized delivery merges keep using the Gitea merge endpoint
  through `GiteaApi.request`, under the rules the task prompt gives. No command approves
  a review: `pr-review-create` refuses `APPROVED`.
- No users, organizations, tokens, secrets, webhooks, repository settings, admin, or
  repository-delete commands. No generic "send this HTTP request" command.
- No token parameter or credential storage. No direct network fallback when the proxy
  is missing.
- No artifact extraction. `artifact-download` saves the zip bytes as they are.
- No live rollout. Merging this code changes no running worker (see 6).

# 3. Decisions Made

## 3.1 Command matrix

`number` is the per-repository issue/PR index. `*_id` is a global id. Output fields keep
the two apart: `issue_number`, `issue_id`, `comment_id`, `review_id`, `review_comment_id`,
`run_id`, `run_number`, `job_id`, `artifact_id`, `label_id`, `status_id`. Gitea's raw
fields stay next to them. `--pr` is an alias of `--number`.

| Area | Command | Notes |
| --- | --- | --- |
| Existing | `create`, `get`, `issue-get`, `list-prs`, `update`, `status` | Same arguments and output shapes. `list-prs` still prints an array; `--all` pages it. `create` and `update` print the PR read back. |
| Repository | `repo-get`, `default-branch`, `branch-get --branch B` | `default-branch` → `{default_branch, sha}`; `branch-get` → `{branch, sha, protected, commit}`. |
| Issues | `issue-list`, `issue-create`, `issue-update`, `issue-close`, `issue-reopen` | `issue-list --type issues` (the default) leaves out PRs; `--type pulls\|all`, `--state`, repeated `--label`, `--q`, `--since`, `--before`. |
| Labels | `label-list [--number N]`, `label-add`, `label-remove` | Labels are given by exact name (repeat `--label`); unknown names are refused. |
| Comments | `comment-list`, `comment-get`, `comment-create`, `comment-edit`, `comment-delete` | Work on issues and PRs alike. Always print `comment_id`. `comment-create --idempotency-key K` adds `<!-- argus-key:K -->` and returns the existing comment when the marker is already there. |
| PR details | `pr-commits`, `pr-files`, `pr-diff` | `pr-diff` prints the raw diff (`--max-bytes`, default 5 MiB). |
| Reviews | `pr-review-list`, `pr-review-get`, `pr-review-comments`, `pr-review-create` | `pr-review-get` includes inline comments. `pr-review-comments` collects the inline threads of every review. `pr-review-create` takes `COMMENT` or `REQUEST_CHANGES`, `commit_id` defaults to the PR head, and `--comments-file` holds JSON inline comments. |
| Statuses | `statuses --sha S` | Paginated statuses plus `combined_state`. |
| Actions | `run-list`, `run-get`, `job-list`, `job-logs`, `artifact-list`, `artifact-download` | `run-list --branch/--head-sha/--status/--event`. `job-logs` prints text, or saves raw bytes with `--out` (default 20 MiB). `artifact-download --out` is required (default 200 MiB). |
| Review gate | `review-findings --pr N [--author LOGIN]` | See 3.4. |

`argus-gitea-api --help` and `<command> --help` list every option.

## 3.2 Arguments, bodies, output

- Options are `--name value` or `--name=value`, split at the first `=` only. Values are
  never split on commas or trimmed. Unknown options, repeated single options, and missing
  values are usage errors. The old parser ignored them silently.
- Bodies come from `--body TEXT`, `--body-file PATH` (a regular file), or `--body-stdin`,
  one of them at most, up to 256 KiB. They are sent byte for byte, so Unicode, newlines,
  commas, and `=` survive.
- Lists take `--page`, `--limit` (1..50), and `--all`, with `--max-items` (default 1000,
  at most 10000) and `--max-pages` (default 50). Pages are requested by number from the
  repository base URL; a `Link` URL is never fetched. A page has more after it when
  `Link` has `rel="next"`, else when `x-total-count`/`total_count` says so, else when the
  page is full. New list commands print
  `{items, count, total_count, page, pages_fetched, limit, has_more, next_page, truncated}`.
- `--format json` is the default. `--format text` prints one `key=value` line per item.
- Logs, diffs, and artifacts are read as bytes and never parsed as JSON. On stdout they
  are decoded as UTF-8 with replacement characters; `--out` keeps the exact bytes.

## 3.3 Errors and exit codes

Errors are one JSON line on stderr: `{"error":{"code","status","method","path","message"}}`.
`path` is the API path without its query. The message is short and sanitized: token-like
strings and URL userinfo are removed. The body of a 401 or 403 is never echoed, and
argument values are never echoed.

| Exit | Code | Cause |
| --- | --- | --- |
| 0 | — | success |
| 1 | `internal_error` | unexpected failure |
| 2 | `usage` | bad or missing option, invalid id, ref, or SHA |
| 3 | `unauthorized` | HTTP 401 |
| 4 | `forbidden` | HTTP 403 |
| 5 | `not_found` | HTTP 404 |
| 6 | `conflict` | HTTP 409 |
| 7 | `validation_failed` | HTTP 422 |
| 8 | `rate_limited` | HTTP 429 |
| 9 | `server_error` | HTTP 5xx |
| 10 | `network_error` | connection failure or timeout |
| 11 | `http_error` | any other non-2xx |
| 12 | `limit_exceeded` | download larger than `--max-bytes` |
| 13 | `policy_denied` | repository outside the allowlist, plain HTTP, missing proxy, refused redirect, approval, path guard, unsafe `--out` |
| 14 | `ambiguous` | the mutation outcome could not be confirmed (for example, a deleted comment still exists) |
| 15 | `bad_response` | a response with the wrong shape |

Retries happen only for GET and HEAD, on 429, 5xx, or a network error. There are at most
3 retries, with exponential backoff (500 ms × 2ⁿ, equal jitter). `Retry-After` is honored
up to a 10 s cap. POST, PATCH, PUT, and DELETE are sent once. When a mutation fails with
a network error or 5xx, the helper reads back to find out what happened: the open PR for
the head and base, the comment with the marker (or the same body in the last 10 minutes),
the issue with that title and body, the edited fields, the labels, the review with that
body and `commit_id`, or a 404 for a delete. A confirmed outcome is returned with
`reconciled: true`. Otherwise the error is returned with "not retried". Every
create/edit/close/reopen prints the object read back. `comment-delete` prints
`{comment_id, deleted: true, verified: true}` only after a 404.

## 3.4 `review-findings`

The command reads the PR head SHA, every comment on the PR, and the statuses on that SHA.
A comment counts as an agentic review when it mentions "agentic review". From its text the
command parses the reviewed head SHA (`head`, `reviewed head`, `commit`, or `sha`, followed
by 7–64 hex characters) and the finding count (`finding_count: N`, `findings: N`,
`N … findings`, or "no findings" for 0). It chooses the newest comment whose SHA is a
prefix (at least 7 characters) of the PR head. The output is
`{pr_number, head_sha, comment_id, comment_head_sha, matches_head, finding_count, body, status, reason}`.
`status` is the newest status whose context names the agentic review. If no comment
matches the head, or the matching comment has no count, `finding_count` is `null` and
`reason` explains why. A green status or job is never reported as zero findings.

## 3.5 Security model

- No credentials. Every request goes through the OneCLI HTTPS proxy (Undici
  `ProxyAgent`), which adds auth for the exact repository paths. Without the proxy the
  helper fails closed (exit 13). Plain HTTP is refused.
- The repository must be an exact entry of `ARGUS_REPOSITORIES_JSON`, normalized to
  `https://host/owner/repo` (no credentials, query, or extra segments).
- Every path segment comes from a validated value: positive integers for numbers and ids,
  7–64 hex characters for SHAs, a safe ref charset with no `..` for branches, all encoded
  with `encodeURIComponent`. A final guard refuses any API path with `..`, `%2e`, `%2f`,
  `\`, `//`, or an absolute URL. Cross-repository access cannot be expressed.
- API requests use `redirect: "error"`. Downloads follow at most 3 redirects by hand,
  and only when the next URL is HTTPS, same origin as the allowlisted repository
  (`this.repository.origin`), and has no userinfo. Follow requests send only an
  `Accept` header (no `Authorization` or `Cookie`). Any other host, scheme, or
  credentialed URL is refused with `policy_denied`.
- `--out` must not contain `..` and must not be a symlink. It must not exist, or must be
  an empty regular file. The bytes go to an exclusive `0600` temp file in the same
  directory, which is renamed or hard-linked into place, so an existing file is never
  overwritten. No archive is extracted.

# 4. Open Questions

- Gitea versions differ in their Actions endpoints (`/actions/runs`, jobs, logs,
  artifacts). The helper follows the current REST v1 shapes. An older server answers 404,
  which shows as `not_found`.
- The agentic-review comment format is not formally specified. The parser is tolerant on
  purpose, and a stricter machine-readable marker would make the gate exact.

# 5. Constraints

- One file plus its `.d.mts`, only `undici`, Node 22. `install.sh`,
  `engines/Dockerfile.tools-layer`, `apply-tools-layer.sh`, `build-project-image.sh`, and
  the project Dockerfiles copy the file unchanged, and that copy is idempotent.
- Existing callers (`argus-delegate` CI probe, continuation observer) use `GiteaApi`,
  `request`, `getPullRequest`, and `createGatewayFetch`. Their signatures are unchanged;
  the constructor only gained an optional fourth `options` argument.
- Tests (`integrations/nanoclaw/gitea-api.test.mjs`, fixtures in
  `integrations/nanoclaw/fixtures/gitea-api/`) are offline and use a mocked fetch or a
  local proxy. They run in `pnpm test`.
- Installer acceptance is
  `integrations/nanoclaw/tests/test_gitea_api_install.py`, collected by `uv run pytest`.
  It exercises the three install shapes (`install.sh`, `engines/install-engines.sh`, and
  `Dockerfile.tools-layer` / `apply-tools-layer.sh`) by double-installing into isolated
  temporary roots, asserting byte-identical copies of `gitea-api.mjs` as
  `argus-gitea-api`, and that a missing OneCLI proxy fails closed with exit 13. It also
  guards against installer script drift: the real `install.sh`,
  `engines/install-engines.sh`, and `engines/Dockerfile.tools-layer` (and/or
  `apply-tools-layer.sh`) must still contain the expected `cp` / `install -m` /
  `COPY --chmod` lines that place `gitea-api.mjs` at `/usr/local/bin/argus-gitea-api`.

# 6. Implementation Consequences

- `renderTask` lists the commands for workers. Images built before this change keep the
  old six commands and ignore unknown options, so a worker checks `argus-gitea-api --help`
  on its image before it relies on a new command.
- **Merged code is not a live rollout.** Running agent containers keep the helper their
  image was built with. Rollout is an operator step:
  1. Rebuild the agent or project image (`provision.sh`, `build-project-image.sh`, or the
     tools layer) and confirm `cmp` of the installed `/usr/local/bin/argus-gitea-api`
     against the source. The installer pytest above is the offline gate that the copy
     steps stay wired; image rebuild remains the live delivery step.
  2. Canary on one worker: run `argus-gitea-api --help`, then read-only `repo-get`,
     `issue-list --limit 1`, and `review-findings --pr <open PR>`. Check the exit codes and
     that there is no credential in stdout or stderr.
  3. Recreate the remaining worker instances. Roll back by redeploying the previous image.
