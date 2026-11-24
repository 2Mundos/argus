---
status: implemented
created: 2026-11-24
updated: 2026-11-24
---

# Forge integrations: repository credentials per project

# 1. Purpose and Scope

Gitea and GitHub tokens are provider **accounts** today: global rows in the
account pool, leased by every task through a `repository` lease role, and listed
on the Accounts screen beside model credentials. They have none of the things
Accounts is built around (quota, usage, exhaustion, cloud and local tiers,
engine ranking), so the account code carries exceptions for them in eligibility
(`role_accepts`, `ENGINE_PROVIDERS`), routing, cost and the dashboard. They are
also scoped too broadly: one `gitea` token is sent to every Gitea host a PR or
lease names, and the token Argus picks is the highest-priority one of the
provider, not the one meant for the repository.

This spec moves repository credentials to a third [integration](integrations.spec.md)
kind, **`forge`**: one per project, bound to the host of the project's
repository, and delivered to every attempt of that project's repository-backed
tasks. Accounts become inference-only.

In scope: the `forge` kind (record, validation, API, dashboard), the worker route
that replaces the `repository` lease, the PR reads Argus makes itself, migration
of existing `gitea` and `github` accounts, and removal of the repository role
from the account pool.

# 2. Non-Goals

- No change to what a worker can do with repository access: the OneCLI secrets,
  the GitHub spelling alias, the preflight, `argus-gitea-api`,
  `argus-github-api`, `repository:write` and [project code
  availability](project-code-availability.spec.md) keep their behaviour. Only
  where the token comes from changes.
- No GitHub App, no GitHub Enterprise Server, no OAuth: still a personal access
  token per [GitHub repositories](github-repositories.spec.md).
- No per-task selection of a forge integration and no MCP tool for one (as for
  every integration, a secret never passes through a model's context).
- No more than one active forge integration per project.
- No repository access for the `global` project, as today.

# 3. Decisions Made

## 3.1 Accounts are inference only

`gitea` and `github` stop being account providers. `POST /api/accounts` with
either, and replacing the credential of a moved one (3.6), is `422
forge_provider_moved`, whose message points to Setup → Integrations. The lease
role `repository` is removed (`LeaseRole` is `engine` | `executor`) and no forge
account is ever leased. The only exceptions left are the ones that keep moved
accounts out of model capacity: task routing, the cost report and the dashboard's
usage view skip them. The provider of a forge host is still decided only by
`repository_provider` (`github.com` is `github`, every other host `gitea`).

## 3.2 The `forge` record

A `forge` integration uses the record, secret, expiry, compensation and event
rules of [integrations](integrations.spec.md) 3.1, with:

- `config`: `{host, username?}`. `host` is the forge host as the project's
  repository spells it, lowercased. `username` is required for a Gitea host
  (Basic authentication, no `:`, CR or LF) and refused for `github.com`.
- Secret `{token}`, single-line, as for the other kinds.
- `access`: `read_write` (default: most repository-backed tasks publish) or
  `read_only`.

Create requires that the project has a repository (its `repository_url`, else its
entry in a pool's `nanoclaw.repositories` map, as [project code
availability](project-code-availability.spec.md) resolves it), else `422
project_repository_missing`, and that `host` is that repository's host, else
`422 forge_host_mismatch`. A second active forge integration in the project is
`409 forge_exists`. `PATCH` may change `username` and `access`, never `host`:
moving to another forge is revoke and create. Rotation and revocation are those
of integrations 3.4.

The token is bound to its host. A token is never sent to a host other than its
record's, by a worker or by Argus.

## 3.3 Validation

`POST …/validate` sends one authenticated GET of the project's repository to the
record's host, outside any write transaction, with the address, DNS-pinning,
no-redirect and log-suppression rules of integrations 3.5:

| Host | Request |
| --- | --- |
| `github.com` | `GET https://api.github.com/repos/{owner}/{repo}`, `Authorization: Bearer <token>` |
| other (Gitea) | `GET https://{host}/api/v1/repos/{owner}/{repo}`, Basic `username:token` |

The result is `ok` when the answer is 200, its `full_name` is the repository's
`owner/repo` (letter case aside), and it carries a `permissions` object, which
both forges return only to an authenticated caller, with `push` true for
`read_write` or `pull` true for `read_only`. Anything else is `failed` with a
fixed detail (`token rejected`, `repository not found for this token`, `token
lacks push access`, `cannot prove the token was accepted`, `forge unreachable`).
A detail never quotes a response.

## 3.4 Delivery: implicit, per attempt

Forge integrations are the one kind a task does not select. The grant is the
project's repository plus the task's capabilities, exactly as today: every
attempt that resolves a repository gets the project's forge integration, read or
write per `repository:write`. `POST /api/tasks` and `PUT
/api/tasks/{id}/integrations` treat a forge id as an invalid selection (`422
integration_invalid`), and routines cannot carry one.

The `repository` account lease is replaced by a worker route, outside the
generated contract like the other integration worker routes (worker token only,
`Cache-Control: no-store`):

`POST /api/tasks/{id}/repository/credential` (`{attempt, secret_version?}`)

It runs in one write transaction and checks, in order:

1. The caller holds the task's lease for exactly that attempt.
2. The task resolves a repository (else `409 project_repository_missing`).
3. The task's project has an active forge integration whose `host` is that
   repository's host (else `409 forge_missing`).
4. Its `access` is `read_write` when the task lists `repository:write` (else
   `409 forge_read_only`).
5. Readiness as integrations 3.6, step 3, except validation: `failed` is `409
   integration_unvalidated`, but `unchecked` is delivered. Every attempt's
   repository preflight proves access anyway, and a migrated record (3.6) must
   not block work until someone presses Validate.
6. `secret_version`, when given, matches the row (`409 integration_changed`).

There is no engine check: OneCLI injects repository access for every engine and
executor (Claude, Codex, Cursor, Copilot, Google, custom, local, dsh), as today.
The response is `{integration_id, provider, host, username?, token,
secret_version, config_version, access}`. Success records
`task.integration_delivered` as integrations 3.6 does; errors carry fixed text.

The NanoClaw channel calls this route where it requested the `repository` lease
and applies the OneCLI secrets from its answer unchanged ([GitHub
repositories](github-repositories.spec.md) 3.3). Any error blocks the task before
a model runs with `env_repository_unavailable` and an `error` naming the cause
(for example `no forge integration for github.com in project argus`). Release
still unassigns every repository secret when the task ends. The reconciled set of
[worker lifecycle](worker-lifecycle.spec.md) is the engine and executor leases
plus the attempt's repository credential.

A running attempt keeps the token it was given; rotation and revocation take
effect at the next attempt. As for every integration, removing access in Argus
does not revoke the token upstream.

## 3.5 PR reads by Argus

The PR checker of [task delivery](task-delivery.spec.md) and task prerequisites
read a PR with the active, not `failed`, forge integration of a project whose
repository is the PR's repository and whose `host` is the PR's host (the first
such project by slug, when several share one); otherwise with none, which reads
as `unknown` like any other failed read. A token never goes to another host or
repository. This replaces "the highest-priority enabled account of the
provider". A read is cached before its flight ends, so a caller arriving as it
finishes reuses it instead of starting another.

## 3.6 Migration

Migration `0090_forge_integrations.sql` rebuilds `integrations` to accept kind
`forge` and adds a unique partial index on `project` for active `forge` rows.

Moving the tokens needs the secret store, so it is a one-time step at API start,
after migrations, through the same create service as the API. For each non-global
project that resolves a repository and has no active forge integration, it takes
the highest-priority enabled account of the repository's provider whose secret
loads, and creates `forge` integration `forge` for that host, with the account's
`username` (Gitea), `access` `read_write` and validation `unchecked`; the event
`integration.created` carries `migrated_from` (the account id). A project with no
matching account gets nothing and its tasks block as in 3.4 until an admin adds
one. Every Gitea project gets the best Gitea token, whatever its host, as the
account pool gave it before; validating each record shows where it does not
fit. Re-running skips projects already done. It runs only when the secret store
loaded; otherwise at the next start.

The step then disables every `gitea` and `github` account (reason `moved to forge
integrations`) and releases any `repository` lease still open. The accounts are
not deleted: the Accounts screen marks them as moved, with a link to
Integrations and only Delete, and a downgrade can re-enable them by hand.
Deleting one is the admin's call.

## 3.7 Dashboard

**Setup → Integrations.** Add integration offers kind **Forge (repository
access)**. The form shows the project's repository and its provider, read-only,
takes the username for a Gitea host and the token, and offers intended access.
It cannot be saved for a project without a repository, and links to the project
to set one; a second active one is refused with a fixed message.
The table's target column shows the repository. The screen's explanation says a
forge integration reaches every repository-backed attempt of the project without
selection, unlike MCP and Kubernetes ones. The token field follows integrations
3.7.

**Task detail and New task** do not list forge integrations as choices. The task
detail shows the repository access the task gets: the integration's name and
`read` or `write`, or why an attempt would block (none, read only for a write
task, revoked, expired, validation failed).

**Accounts** drops the `gitea` and `github` options, their forms and the "Other"
tile's mention of them, and shows the moved accounts of 3.6.

# 4. Open Questions

1. Should a project allow a `read_only` and a `read_write` forge integration side
   by side, so read-only tasks never hold a token that can push? This draft keeps
   one per project.
2. Should the disabled accounts of 3.6 be deleted automatically one release
   later?

# 5. Constraints

- Single node: no new service; secrets stay in the SOPS file under
  `integration-<id>` ([single node](single-node.spec.md)).
- Validation and the migration step make no network call or SOPS write inside a
  write transaction; all time is `tx.now`.
- `0090` is a new migration; no shipped migration is edited.
- Tests cover: create rules (no repository, host mismatch, second active record,
  username for Gitea only), validation outcomes against a fake forge, delivery
  binding (stale attempt, missing record, read-only record for a write task,
  revoked, expired, `failed` refused, `unchecked` delivered), forge ids refused as
  task selections, PR reads never crossing hosts, the migration step (copy,
  idempotent re-run, unmatched project, accounts disabled), and the account
  provider refusal.
- No MCP tool description changes are expected; `make tool-tokens` confirms it in
  the PR.

# 6. Implementation Consequences

- `packages/core`: `LeaseRole` without `repository`; `role_accepts`,
  `ENGINE_PROVIDERS` and `REPOSITORY_PROVIDERS` simplified (the latter becomes
  the forge provider set of the integration kind).
- `apps/api`: migration `0090`; `forge` config and validation in the integrations
  service; the worker credential route; task selection refusing forge ids; the
  startup migration step; `PrChecker.login` and prerequisites by project and host;
  account create refusing `gitea` and `github`; the repository exceptions removed
  from accounts routes and service, cost and task routing; `make openapi`.
- `apps/dashboard`: forge form and row in Integrations; repository access line in
  task detail; Accounts without forge providers, with the moved-accounts list;
  `accountProviders.ts` and `usage.ts` without forge exceptions.
- `integrations/nanoclaw`: `argus-accounts.ts` and `argus.ts` request the
  repository credential from the new route; the reconciled lease set.
- Specs that follow it: [integrations](integrations.spec.md) (three kinds, the
  selection exception), [GitHub repositories](github-repositories.spec.md),
  [worker lifecycle](worker-lifecycle.spec.md) repository credentials, [account
  selection](account-selection.spec.md) eligibility, [project code
  availability](project-code-availability.spec.md) and [failure
  classes](failure-classes.spec.md) (credential wording).
- Rollout: deploy the API and the NanoClaw channel together (an old channel asks
  for a `repository` lease that no longer exists and blocks with
  `env_repository_unavailable`); check Integrations for every repository-backed
  project after the first start.

## Verification boundary

- Commands:
  - `uv run pytest -q apps/api/tests/test_forge_integrations.py apps/api/tests/test_task_delivery.py apps/api/tests/test_accounts.py`
  - `pnpm --filter dashboard exec vitest run src/views/Integrations.test.tsx src/views/TaskIntegrations.test.tsx src/views/Accounts.test.tsx src/views/AccountActions.test.tsx`
  - `integrations/nanoclaw/provision.sh check` (the channel's vitest list, including
    `argus-accounts.test.ts`).
- Verified: create rules, validation against a fake forge, delivery binding and
  refusals, forge ids refused as selections, PR reads not crossing hosts, the
  start-up move (copy, re-run, unmatched project, accounts disabled, leases
  released), the account refusals, the channel's route call and its blocks, and
  the dashboard forms, all with fakes.
- Not verified: a real Gitea or GitHub token, the start-up move on the preview
  install's data, a worker attempt through the new route on a real node, the
  dashboard in a real browser.
