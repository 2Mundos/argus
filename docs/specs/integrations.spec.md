---
status: partial
created: 2026-11-12
updated: 2026-11-12
---

# Integrations: MCP connections and Kubernetes clusters

# 1. Purpose and Scope

An **integration** is an outside-service connection managed by an admin for one
project. Credentials are saved and validated; a Kubernetes connection reaches a task
worker only when that task opts in (3.6). MCP credentials are not delivered to
workers. Two kinds:

- **`mcp`**: a remote MCP server over HTTPS with a bearer token.
- **`kubernetes`**: a service-account token with a configured target namespace of
  one cluster and intended access, defaulting to read-only. Validation rejects
  sampled broad permissions; it does not prove the token's grants are confined
  to that namespace.

They are managed in **Setup → Integrations** (`#setup/integrations`) and the REST API,
apart from provider [Accounts](claude-accounts.spec.md) (model and forge credentials
agents lease) and typed [Settings](settings.spec.md) (which never hold a secret).

# 2. Non-Goals

- No secret in SQLite, a setting, a task spec or input, a knowledge page, a pool's
  `env`, an event, a log, an artifact, browser storage or an API response.
- No global injection: an integration never reaches every worker, the orchestrator,
  or a task of another project, and never a worker's `~/.kube` or process-wide
  `KUBECONFIG`.
- No MCP tools for integrations: creating or rotating one through MCP would pass the
  secret through a model's context. Admins use the dashboard or REST.
- No change to Kubernetes pools: a pool's kubeconfig context lets Argus place worker
  pods ([worker lifecycle](worker-lifecycle.spec.md)); it gives tasks no cluster access.

# 3. Decisions Made

## 3.1 Record and secret

Table `integrations`: `id`, `project` (FK, never `global`), `kind`, `name` (unique
among the project's active integrations, `^[a-z0-9][a-z0-9-]{0,62}$`), `config`
(non-secret JSON, 3.2/3.3), `access` (`read_only` | `read_write`), `secret_ref`,
`secret_version`, `state` (`active` | `revoked`), `validation` (`unchecked` | `ok` |
`failed`), `validation_detail`, `validated_at`, `created_at`/`created_by`,
`updated_at`, `rotated_at`, `revoked_at`/`revoked_by`, `expires_at`.

`expires_at` is when the upstream credential stops working, or null when unknown.
Create and rotate take an optional `expires_at` (with a timezone, in the future) and
store the earlier of it and the token's own JWT `exp` claim, when it has one; rotate
replaces the previous value. Delivery refuses an expired integration (3.6).

The secret is `{token}` (1–8192 printable characters, no whitespace) in the SOPS file
as `integration-<id>` ([single node](single-node.spec.md) 3.6); the row holds only
`sops:integration-<id>`, and a revoked row holds none. SOPS and SQLite cannot commit
together: create writes the secret first; rotate and revoke retain an independent
copy of the prior credential before mutating either store. All three operations
finish their store mutation, database commit or rollback, and any compensation
before delivering caller cancellation. This also covers SOPS subprocesses and
filesystem threads, which must finish before rollback can overwrite their effects.
Cancellation immediately before or after COMMIT can therefore leave a completed
operation; callers must re-read the record. Audit rows commit with the state change,
and committed events are published before cancellation reaches the caller.

Compensation requires a serialized database readback: an absent create row permits
removing its orphan secret; the exact prior active state and secret version permit
restoring a failed rotate/revoke. A committed create keeps its secret, a committed
rotation keeps its new token, and a committed revocation never restores a token.
An indeterminate database outcome does not authorize destructive compensation.
These guarantees cover request cancellation, not atomic recovery across a machine
crash or simultaneous permanent failures of both persistence stores.

When the application reports the store unavailable, create, rotate, revoke and
validate return sanitized `503 secret_store_unavailable` without changing the row
or recording a success event. Rotate, revoke and validate also require the
referenced credential to be loaded and present; a missing reference is the same
store error, not an upstream validation failure. Revoke requires confirmed deletion
before clearing the reference or recording success. Admin and project checks retain
their normal `401`/`403`/`404` behavior. Tokens containing LF or CRLF are rejected
before any network access, including credentials read back from the store.

## 3.2 MCP connections

`config`: `{url}`, an `https://` URL without user info, query or fragment (a token in
a URL would be stored in plain text). Authentication is a bearer token. `access`
records what the admin intends the server's tools to do; Argus cannot narrow an MCP
server's tools, so it is a label, shown as such.

**OAuth is not available.** The MCP authorization flow needs a registered redirect
URL, a refresh loop, and re-delivery of refreshed tokens to an attempt that is
already running; none exists. The screen does not offer it.

## 3.3 Kubernetes clusters

`config`: `{server, namespace, ca_data?}`: an `https://` API server URL, a DNS-1123
namespace, and an optional base64 PEM CA bundle (public, not secret). `access`
defaults to `read_only`. Namespace selects the intended target, not the scope of
the token's actual grants. Validation (3.5) checks explicit permissions only;
`ok` does not attest to complete RBAC isolation or absence of every write permission.

## 3.4 API

Admin token only (`403` otherwise); a missing token is `401`.

- `GET /api/integrations`: every project's integrations, revoked included.
- `GET /api/projects/{slug}/integrations`, `POST` same path (create).
- `GET /api/projects/{slug}/integrations/{id}`, and `POST …/{id}/validate`,
  `…/{id}/rotate` (`{secret, expires_at?}`), `…/{id}/revoke`.
- `PUT /api/tasks/{id}/integrations` (`{expected_version, integrations}`): a task's
  opt-in (3.6). The task's `integrations` field lists the selected ids.

An id outside `{slug}` is `404`, the same as an unknown id. Responses never carry the
secret: they say `has_secret`. Rotating or revoking a revoked integration is `409
integration_revoked`. Every change records an event with subject `integration` and no
secret: `integration.created`, `.validated` (`{status, detail}`), `.rotated`, and
`.revoked`. Opt-in and delivery record task events (3.6).

Two worker routes are left out of the generated API contract: only a NanoClaw
channel calls them, with a worker token (`403` for admin and orchestrator tokens):

- `GET /api/tasks/{id}/integrations/status?attempt=N`: non-secret; each selected
  integration's id, name, namespace, `secret_version`, `expires_at` and `readiness`.
- `POST /api/tasks/{id}/integrations/{iid}/credential` (`{attempt, secret_version?}`):
  the only response that carries a token, with `Cache-Control: no-store`.

## 3.5 Validation

`POST …/validate` makes the network check outside any write transaction, then records
`ok` or `failed` with a short detail and `validated_at`, unless the secret was rotated
or revoked meanwhile (`409 integration_changed`). A detail never quotes a response
body or header, which could echo the token. Credentialed checks suppress the
HTTP libraries' trace logs in their own async context, including response headers,
reason phrases and exceptions, even when HTTP DEBUG logging is enabled. Unrelated
HTTP logging remains enabled.

- **MCP**: send JSON-RPC 2.0 `server/discover` with a matching response ID required,
  the `io.modelcontextprotocol/protocolVersion`, `io.modelcontextprotocol/clientInfo`
  and `io.modelcontextprotocol/clientCapabilities` request `_meta`, and
  `MCP-Protocol-Version: 2026-07-28` / `Mcp-Method: server/discover` headers.
  Accept both `application/json` and `text/event-stream`, including SSE notifications
  before the final response. A successful result requires `resultType: complete`,
  a nonempty string list `supportedVersions`, and an object `capabilities`. If the
  list includes `2026-07-28`, validation succeeds without legacy initialization.
  Otherwise, choose the newest mutually supported legacy version in this order:
  `2025-11-25`, `2025-06-18`, `2025-03-26`, then attempt `initialize` with that exact
  version. A list with no mutually supported version fails validation. A bare `2xx`,
  JSON-RPC error, malformed body, wrong ID or incomplete result fails with sanitized
  detail and never enables version negotiation.

  Follow the official [discovery](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/server/discover.mdx),
  [versioning](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/versioning.mdx)
  and [Streamable HTTP](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/transports/streamable-http.mdx)
  contracts. An HTTP `400` without a recognized modern error permits one legacy
  `initialize` attempt, preferring `2025-11-25` when no version list is available.
  `404`/`405` also permit that probe for a legacy endpoint or unknown method; no
  deprecated HTTP+SSE GET transport is attempted. `401`/`403`
  and recognized modern errors `-32020`, `-32021`, `-32022` never trigger fallback.
  Legacy success requires matching JSON-RPC ID and a result with the exact requested
  `protocolVersion`, object `capabilities`, and nonempty `serverInfo` name/version
  strings. Legacy `initialize` keeps bearer authentication and the JSON/SSE `Accept`
  header, but omits `Mcp-Method` and `MCP-Protocol-Version`: the 2025
  [Streamable HTTP rules](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#protocol-version-header)
  require the version header on subsequent requests after initialization.
  HTTP `200` JSON-RPC errors never trigger fallback.

  Success detail is **"MCP response valid; credentials not explicitly rejected."**
  A public server or one ignoring Authorization can return a valid response, so
  this does not prove token acceptance or authorization. `401`/`403` means the
  credentialed request was rejected. No upstream text is copied into details.
- **Kubernetes**: `SelfSubjectAccessReview`s with the token, verifying TLS with
  `ca_data` (else the system store). It must be allowed to `list pods` in the
  configured target namespace. The following checks omit `namespace` and must
  be denied, otherwise validation is `failed` with a fixed "too broad" detail:

  - `get`, `list` and `watch` on core `pods`, `secrets`, `configmaps`; apps
    `deployments`, `statefulsets`, `daemonsets`; batch `jobs`, `cronjobs`.
  - `create`, `patch` and `delete` on core `pods`.
  - `create` on core `pods/exec`, `pods/attach`, `pods/portforward` and
    `serviceaccounts/token`.
  - `create` on RBAC `clusterrolebindings`, and `*` on group `*`, resource `*`.

  For `read_only`, `create`, `patch` and `delete` on `pods` in the target
  namespace must also be denied. Every probe must return an object `status`
  with an explicit boolean `allowed` and no `evaluationError`. Malformed or
  indeterminate authorization replies fail with the fixed detail "invalid Kubernetes
  authorization response"; they never count as denials on negative probes.

  The Kubernetes [ResourceAttributes contract](https://github.com/kubernetes/api/blob/release-1.37/authorization/v1/types.go)
  defines an empty or omitted namespace in a SAR/SSAR as all namespaces for a
  namespaced resource; an omitted name means all resource names. These probes
  request authorization decisions only, never workload or secret contents.

  Success means only these explicit checks passed. This finite sample cannot
  establish least privilege: permissions limited to another namespace or resource
  name, other resources, verbs, subresources, API groups and escalation paths may
  remain unchecked. `read_only` and `read_write` record intended access, not a
  complete permission inventory. A namespace-only credential must be provisioned
  and reviewed independently of this validation.

Rotation resets validation to `unchecked`.

## 3.6 Worker delivery

**Opt-in.** A task selects Kubernetes integrations of its own project by id (at most
8); selection is the only grant. Capabilities, titles, pool labels and the project
itself never give a task a credential. `POST /api/tasks` takes the same list as
`integrations`, from an admin token only (`403 integration_admin_only`), with the
same checks. The admin-only `PUT` (3.4) checks the task's
`version` (`409 stale_version`) and that each id is an active `kubernetes`
integration of the task's project; one `422 integration_invalid` names every bad id,
and an id of another project reads as unknown. The selection changes only while the
task is `awaiting_approval`, `pending`, `ready` or `blocked` and not archived (`409
task_not_editable`): a running attempt keeps the selection it started with. A change
records `task.integrations_changed` (`{added, removed}`). Clearing the list is the
way back. MCP and the orchestrator have no selection tool.

**Delivery binding.** Both worker routes require the caller to hold the task's
current lease for exactly that `attempt` ([worker lifecycle](worker-lifecycle.spec.md)
3.3); a stale attempt or another worker gets the lease error. The credential route
runs inside one write transaction, so it serializes with rotate and revoke, and
checks in order:

1. The integration is selected by the task and of the task's project (`403
   integration_not_selected` otherwise).
2. The worker's leased engine account, and its leased executor if any, are Claude
   (`anthropic`); otherwise `409 integration_engine_unsupported`.
3. Readiness: `revoked` (`409 integration_revoked`), `expired` (`409
   integration_expired`), validation not `ok` (`409 integration_unvalidated`), the
   store down (`503 secret_store_unavailable`), the stored secret missing (`503
   integration_secret_missing`) or behind the row's version (`503`).
4. `secret_version`, when given, matches the row (`409 integration_changed`): a
   worker never receives an older token than the status it read.

Success records `task.integration_delivered` (`{attempt, integration_id, name,
secret_version}`), never the token. Errors carry fixed text and no secret. The status
route reports the same readiness except the store-version check: rotate writes the
store inside its transaction, so a status read may see the new token beside the old
row, and only delivery decides.

**Installation.** Before the session starts, after the task inputs, the NanoClaw
channel reads the status and fetches every selected credential. Any failure blocks
the task before a model runs, with no attempt spent ([failure
classes](failure-classes.spec.md) 3.3):

| Reason | When |
| --- | --- |
| `env_integration_unavailable` | revoked, expired, unvalidated, missing secret, store down, Argus unreachable |
| `env_integration_unsupported` | the engine or delegated executor is not Claude |
| `env_integration_invalid` | an unsafe directory, a credential that does not match the selection, or a write failure |

The channel writes one kubeconfig per attempt to
`<group>/task-integrations/<task>/<attempt>-<random>/kubeconfig` (in the container,
`/workspace/agent/task-integrations/…`): directories `0700`, the file `0600`. Each
directory is opened with `O_NOFOLLOW` and must be a real, private directory owned
by the channel; the file is written to an exclusive sibling, fsynced and renamed
into place. It holds one context per integration, named after it and pinned to its
namespace, with the token as a bearer token. TLS verifies `certificate-authority-data`
when `ca_data` is set, else the system store; `insecure-skip-tls-verify` is never
written. `~/.kube` is never read, copied or written and no process-wide
`KUBECONFIG` is set.

The worker prompt gets a non-secret hint: the file path, the context names and
namespaces, to run `kubectl --kubeconfig <path>` (or `KUBECONFIG=<path> kubectl`),
and not to print or copy the file. The worker image ships a pinned kubectl whose
download is SHA-256 verified and probed with `kubectl version --client`
([engines](engines.spec.md)).

**Recheck, revocation and expiry.** On each heartbeat the channel reads the status
again. An integration that is no longer `ready` removes the attempt's kubeconfig at
once; a new `secret_version` replaces it with a fresh delivery. Revocation and expiry
therefore take effect within one heartbeat interval. The channel removes the
attempt directory when the attempt ends, on every path (done, failed, blocked,
cancelled, lost lease, crash of the session), so attempts never share a file.
Before its first claim, the channel empties the group's integrations directory, so
a kubeconfig a previous (crashed) process left behind never outlives it.
Deleting the local file does not revoke anything upstream: a token a session
copied stays valid until it expires or is revoked at the cluster. Revoke the
service-account token there too.

**Engine matrix.**

| Engine | Kubernetes integration |
| --- | --- |
| Claude (NanoClaw harness) | supported |
| Claude executor via `argus-delegate` | supported: gets the attempt's kubeconfig as `KUBECONFIG` |
| Codex, Cursor, Copilot, custom, local (as engine or executor) | not supported: delivery refuses and the task blocks with `env_integration_unsupported`; `argus-delegate` refuses a non-Claude executor when a kubeconfig is installed and strips any inherited `KUBECONFIG` |
| Built-in orchestrator | none |
| MCP connections | not delivered to any worker |

The channel refuses as soon as the leased executor is not Claude, even if the task
would not have delegated.

**Limits.** The model's session can read the file: the [command
guard](command-guard.spec.md) blocks Bash commands that print, copy or decode it
(`kubeconfig_secret`) and `kubectl config view --raw`/`--flatten`, but it is accident
prevention, not a boundary; a file-read tool or an encoded command can still read
the token. Confinement to the namespace rests on the token's RBAC (3.3), not on the
kubeconfig.

## 3.7 Dashboard

Setup → Integrations, after Accounts. A project selector excludes `global` and
loads only that project's integrations through the generated API client. Changing
projects discards open forms and their credentials. Loading, empty and error states
include retry actions for failed loads.

The table shows name, kind, target, intended access, state, secret presence,
validation status, detail and time. **Add integration** opens a form for name,
kind, intended access, target and a bearer token. MCP accepts an HTTPS server URL;
Kubernetes accepts an HTTPS API server URL, namespace and optional base64 PEM CA
bundle. It accepts service-account bearer tokens, not kubeconfig uploads.

The token is a write-only password field: it is cleared immediately on submit,
including a failed save or rotation, and is never put in browser storage, errors or
logs. Mutations render only the public connection fields; server error bodies are
replaced with fixed, actionable messages so a token echoed by a server cannot be
shown. An unsuccessful save retains only non-secret fields and needs a fresh token
for retry.

Active rows offer **Validate**, **Rotate** (new token) and **Revoke** (confirm by
typing the name). Pending actions are disabled. Revoked rows remain visible without
these actions. Each row shows its expiry; the add and rotate forms take an optional
expiry in local time. The screen shows secret-store readiness, explains that MCP
credentials are not delivered to workers while Kubernetes ones reach only attempts of
tasks that select them (Claude engines), that removing the local file does not
revoke the token, and that OAuth is unavailable. It links to Accounts, Settings and worker pools without
claiming that pools grant tasks access to these connections.

A task's detail view lists the project's active Kubernetes integrations (and any
already selected) as checkboxes and saves the selection at the task's `version`. It
is read-only while an attempt runs or after the task ends; errors are fixed messages.
Each choice shows its expiry, or why an attempt would block (revoked, expired, not
validated). The New task form offers the project's active Kubernetes integrations,
none chosen by default, and sends the choice as `integrations`.

# 4. Open Questions

- Should the orchestrator see integration names (not secrets) to plan tasks?

## Open items

- MCP OAuth sign-in (3.2).
- Worker delivery of MCP connections (3.6).
- Kubernetes delivery for engines other than Claude (3.6 engine matrix).

# 5. Constraints

- Single node: SQLite plus the SOPS file; no new service.
- Writes through `Database.write()` with `tx.now`; network checks run outside it.
- The dashboard talks to the API only.

# 6. Implementation Consequences

- Migrations `0080_integrations.sql` and `0081_task_integrations.sql` (the
  `task_integrations` table and `integrations.expires_at`); package
  `argus_api/integrations` (service, validation, `selection`, `delivery`, routes);
  `make openapi`.
- Dashboard: `views/Integrations.tsx`, the Setup tab in `App.tsx`,
  `views/TaskIntegrations.tsx` in the task detail.
- NanoClaw: `argus-integrations.ts` (install, recheck, cleanup, prompt hint), wired in
  `argus.ts`; `engines/argus-delegate.mjs` scopes `KUBECONFIG`;
  `engines/install-kubectl.sh` in the tools layer; the `task-integrations` heartbeat
  feature; the command guard's `kubeconfig_secret` rule.
- MCP entry points deliberately exclude credential management and selection
  (section 2). REST and the dashboard share the API services.

## Verification boundary

- Focused backend check: `uv run pytest apps/api/tests/test_integrations.py`.
  The permission matrix includes a target namespace pod-list grant plus
  cluster-wide pod listing, broad secret and workload access, execution and
  token-creation subresources, and namespace-only `read_only` and `read_write`
  positive controls and indeterminate negative-probe responses. MCP coverage
  includes modern JSON/SSE, negotiated legacy versions and preference order,
  unknown-method fallback, initialize headers and exact-version validation,
  authentication and modern errors, malformed responses, and credential echoes through real HTTP core tracing with
  an in-memory network backend. Store readiness, missing references and deterministic
  cancellation immediately before/after COMMIT cover database/secret/audit
  consistency. All upstream traffic uses a fake transport and resolver.
- Focused dashboard checks: `pnpm --filter @argus/dashboard exec vitest run
  src/views/Integrations.test.tsx src/views/TaskIntegrations.test.tsx
  src/App.test.tsx`, dashboard TypeScript checking,
  and Biome on the touched dashboard files. Canonical full gates belong to the parent
  run.
- Component coverage uses a fake fetch transport through the generated client and
  real live-data loading: both forms, project isolation, loading/empty/error states,
  token clearing on success and failure, response/error non-disclosure, validation,
  rotation, revocation confirmation and unavailable actions on revoked records.
- Delivery: `uv run pytest apps/api/tests/test_integration_delivery.py` covers
  selection validation and versioning, the lease binding, the engine matrix, every
  readiness refusal, the version race and the secret-free audit event.
- Channel: vitest `argus-integrations.test.ts` (paths, permissions, symlinks, atomic
  writes, rendering, recheck, cleanup) and `argus-integrations-channel.test.ts`
  (preflight blocks, heartbeat removal, cleanup on every path, delegate passthrough),
  plus `argus-delegate.test.mjs` and `tests/test_command_guard.py`.
- End to end: `ARGUS_TEST_KUBECTL=<kubectl> uv run pytest
  apps/api/tests/test_integration_kubectl_e2e.py` runs the API, the channel module and
  a real kubectl against a local fake Kubernetes API over TLS with a test CA and a
  fake token: allowed reads in the namespace, forbidden other namespaces and writes,
  a request without the token rejected, cross-project refusal, expiry and revocation
  removing the file. Skipped without the variable.
- No real MCP server, Kubernetes cluster, credential or model run is exercised by
  these checks.
