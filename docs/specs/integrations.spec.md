---
status: partial
created: 2026-11-12
updated: 2026-11-13
---

# Integrations: MCP connections and Kubernetes clusters

# 1. Purpose and Scope

An **integration** is an outside-service connection managed by an admin for one
project. Credentials are saved and validated; a connection reaches a task worker
only when that task opts in (3.6): a Kubernetes one as a per-attempt kubeconfig, an
MCP one as a per-attempt server URL on a broker that holds the token. Two kinds:

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
`secret_version`, `config_version`, `state` (`active` | `revoked`), `validation`
(`unchecked` | `ok` | `failed`), `validation_detail`, `validated_at`,
`created_at`/`created_by`, `updated_at`, `rotated_at`, `revoked_at`/`revoked_by`,
`expires_at`.

Responses add `version` (the row's `updated_at`, which every change moves forward;
an edit passes it back, 3.4) and, for MCP, `tool_policy` (3.2). `config_version`
starts at 1 and every config or access edit raises it, as rotation raises
`secret_version`.

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

`config`: `{url, tools?}`. `url` is an `https://` URL without user info, query or
fragment (a token in a URL would be stored in plain text). Authentication is a
bearer token.

`tools` is the admin's explicit allowlist: 1–128 MCP tool names
(`^[A-Za-z0-9_.-]{1,128}$`, case-sensitive), stored sorted and without duplicates.
Argus cannot tell from a server which of its tools only read, so the list is
trusted as the admin wrote it, not verified against the server. Each MCP record
reports a `tool_policy`:

| `tool_policy` | When | A worker may use |
| --- | --- | --- |
| `allowlist` | `tools` is set | only the listed tools |
| `all` | no `tools`, `access` `read_write` | every tool the server lists |
| `unknown` | no `tools`, `access` `read_only` | nothing: tool coverage is unknown |

A `read_only` connection without an allowlist is therefore unusable by workers until
an admin adds one (3.4): a task cannot select it and delivery refuses it. The MCP
broker enforces the policy (3.6); the server's own tool annotations (`readOnlyHint`
and the like) are untrusted and never widen or narrow it. `access` is the intended
access the admin recorded; with an allowlist, which tools only read is the admin's
judgment, not something Argus checks.

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
- `PATCH /api/projects/{slug}/integrations/{id}` (`{expected_version, config?,
  access?}`): corrects the non-secret fields of an active integration. It keeps the
  id, the credential and `secret_version`, and neither reads nor takes a secret.
  `config` replaces the whole config and passes the same checks as create (scheme,
  user info, query, fragment, keys, MCP `tools`; `422 config_invalid`); the
  validation request that follows applies the same address, DNS-pinning and
  no-redirect rules. A stale `expected_version` is `409 stale_version` with the
  current version in `details`; a revoked record is `409 integration_revoked`.
  A change raises `config_version` and resets validation to `unchecked`; saving
  what was read changes and records nothing.
- `PUT /api/tasks/{id}/integrations` (`{expected_version, integrations}`): a task's
  opt-in (3.6). The task's `integrations` field lists the selected ids.

An id outside `{slug}` is `404`, the same as an unknown id. Responses never carry the
secret: they say `has_secret`. Rotating or revoking a revoked integration is `409
integration_revoked`. Every change records an event with subject `integration` and no
secret: `integration.created`, `.validated` (`{status, detail}`), `.updated`
(`{name, config_version, changes}`, each changed key with `before` and `after`; a
CA bundle appears as a `sha256:` digest), `.rotated`, and `.revoked`. Opt-in and
delivery record task events (3.6).

Two worker routes are left out of the generated API contract: only a NanoClaw
channel calls them, with a worker token (`403` for admin and orchestrator tokens):

- `GET /api/tasks/{id}/integrations/status?attempt=N`: non-secret; each selected
  integration's id, `kind`, name, namespace (Kubernetes), `tool_policy` (MCP),
  `secret_version`, `config_version`, `expires_at` and `readiness`.
- `POST /api/tasks/{id}/integrations/{iid}/credential` (`{attempt, secret_version?}`):
  the only response that carries a token, with `Cache-Control: no-store`. It has a
  `kind`: Kubernetes carries server, namespace and CA; MCP carries `url`, `tools`
  (the allowlist, or null with `all`), `tool_policy` and `access`.

## 3.5 Validation

`POST …/validate` makes the network check outside any write transaction, then records
`ok` or `failed` with a short detail and `validated_at`, unless the secret was rotated,
the record revoked, or its config edited meanwhile (`409 integration_changed`): a
result is only about the token and config it checked. A detail never quotes a response
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

Rotation and a config edit reset validation to `unchecked`.

## 3.6 Worker delivery

**Opt-in.** A task selects Kubernetes and MCP integrations of its own project by id
(at most 8); selection is the only grant. Nothing is injected implicitly. Capabilities, titles, pool labels and the project
itself never give a task a credential. `POST /api/tasks` takes the same list as
`integrations`, from an admin token only (`403 integration_admin_only`), with the
same checks. The admin-only `PUT` (3.4) checks the task's
`version` (`409 stale_version`) and that each id is an active integration of the
task's project and, for MCP, not of `tool_policy` `unknown` (3.2); one `422
integration_invalid` names every bad id, and an id of another project reads as
unknown. Validation is checked at delivery, not at selection, so a task can be
prepared while a connection is being fixed. The selection changes only while the
task is `awaiting_approval`, `pending`, `ready` or `blocked` and not archived (`409
task_not_editable`): a running attempt keeps the selection it started with. A change
records `task.integrations_changed` (`{added, removed}`). Clearing the list is the
way back. The orchestrator has no selection tool and no MCP tool selects (section 2).

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
   integration_expired`), an MCP `tool_policy` of `unknown`, from an edit after
   selection (`409 integration_tools_unknown`; readiness `tools_unknown`),
   validation not `ok` (`409 integration_unvalidated`), the
   store down (`503 secret_store_unavailable`), the stored secret missing (`503
   integration_secret_missing`) or behind the row's version (`503`).
4. `secret_version`, when given, matches the row (`409 integration_changed`): a
   worker never receives an older token than the status it read.

The credential carries `config_version`; a channel that receives a different one
than its status read reads the status again. Success records
`task.integration_delivered` (`{attempt, integration_id, name, secret_version,
config_version}`), never the token. Errors carry fixed text and no secret. The status
route reports the same readiness except the store-version check: rotate writes the
store inside its transaction, so a status read may see the new token beside the old
row, and only delivery decides.

**Installation.** Before the session starts, after the task inputs, the NanoClaw
channel reads the status and fetches every selected credential, then writes the
kubeconfig (Kubernetes) and opens broker grants in the session's MCP config (MCP,
see MCP broker). Any failure blocks the task before a model runs, with no attempt spent ([failure
classes](failure-classes.spec.md) 3.3):

| Reason | When |
| --- | --- |
| `env_integration_unavailable` | revoked, expired, unvalidated, MCP tools unknown, missing secret, store down, Argus unreachable; an MCP server refusing the token, redirecting or speaking neither transport |
| `env_integration_unsupported` | the engine or delegated executor is not Claude |
| `env_integration_invalid` | an unsafe directory, a credential that does not match the selection, or a write failure |
| `env_integration_route_unavailable` | the worker has no Kubernetes route or MCP broker, or cannot reach a cluster or MCP server (see Route, MCP broker) |
| `env_integration_gateway_intercepts` | through the route, a cluster's TLS handshake does not verify against its CA or name its host; an MCP server's certificate does not verify against the public roots |

The channel writes one kubeconfig per attempt to
`<group>/task-integrations/<task>/<attempt>-<random>/kubeconfig` (in the container,
`/workspace/agent/task-integrations/…`): directories `0700`, the file `0600`. Each
directory is opened with `O_NOFOLLOW` and must be a real, private directory owned
by the channel; the file is written to an exclusive sibling, fsynced and renamed
into place. It holds one context per integration, named after it and pinned to its
namespace, with the token as a bearer token and the attempt's route as its
`proxy-url` (see Route). TLS verifies `certificate-authority-data`
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
once and revokes its route and MCP grants; a new `secret_version` or
`config_version` replaces them with a fresh delivery and fresh grants, after the
route and MCP probes pass again (a failing probe removes them). A recheck that keeps
them renews the MCP grants. Revocation and expiry therefore take effect within one
heartbeat interval, and sooner for routes and MCP calls, which ask Argus on each use. The channel removes the
attempt directory when the attempt ends, on every path (done, failed, blocked,
cancelled, lost lease, crash of the session), so attempts never share a file.
Before its first claim, the channel empties the group's integrations directory and
removes the session's `argus-integration-*` MCP servers, so nothing a previous
(crashed) process left behind outlives it.
Deleting the local file does not revoke anything upstream: a token a session
copied stays valid until it expires or is revoked at the cluster. Revoke the
service-account token there too.

**Engine matrix.**

| Engine | Kubernetes integration | MCP integration |
| --- | --- | --- |
| Claude (NanoClaw harness) | supported | supported: the session's MCP config |
| Claude executor via `argus-delegate` | supported: gets the attempt's kubeconfig as `KUBECONFIG` | delivered to the session; whether a delegated run sees the session's MCP servers is unverified (Open items) |
| Codex, Cursor, Copilot, custom, local (as engine or executor) | not supported: delivery refuses and the task blocks with `env_integration_unsupported`; `argus-delegate` refuses a non-Claude executor when a kubeconfig is installed and strips any inherited `KUBECONFIG` | not supported: the same refusal and block |
| Built-in orchestrator | none | none |

The channel refuses as soon as the leased executor is not Claude, even if the task
would not have delegated.

**Limits.** The model's session can read the file: the [command
guard](command-guard.spec.md) blocks Bash commands that print, copy or decode it
(`kubeconfig_secret`) and `kubectl config view --raw`/`--flatten`, but it is accident
prevention, not a boundary; a file-read tool or an encoded command can still read
the token. Confinement to the namespace rests on the token's RBAC (3.3), not on the
kubeconfig.

**Route.** A worker session reaches the network through its OneCLI gateway
(`HTTPS_PROXY`), and OneCLI v1.42.0 intercepts every authenticated CONNECT
(`apps/gateway/src/gateway.rs` `handle_connect` sets `intercept = true` whenever an
agent token is present); only an unauthenticated CONNECT is spliced, for any host.
Through the gateway, the cluster's CA does not verify the gateway's certificate,
kubectl fails with x509, and had it not, the gateway would read the bearer token.
`NO_PROXY`, `insecure-skip-tls-verify`, the gateway's CA in the kubeconfig and the
anonymous tunnel each give up what the integration promises, so none is used.

Kubernetes traffic therefore does not use the gateway. The NanoClaw channel runs
its own CONNECT broker (`argus-integrations-route.ts`) that never terminates TLS:

- **Listener.** It binds the worker's docker bridge IPv4, the address sessions
  already reach the host on, at `ARGUS_INTEGRATION_ROUTE_PORT` or a free port. It is
  not configured in pool env or in the session's environment: each cluster in the
  attempt's kubeconfig carries its own `proxy-url`, which kubectl uses for that
  cluster only, so `HTTPS_PROXY` and `NO_PROXY` are unchanged and every other host
  still goes through the gateway.
- **Grants.** For each selected integration the channel opens a grant bound to the
  task, the attempt, the integration and the API server's exact `host:port`. The
  grant is 32 random bytes sent as the `proxy-url` password; the broker keeps only
  its SHA-256. The host is resolved once, at grant time: an address that is
  loopback, link-local, unspecified, multicast or reserved is refused, and the
  tunnel dials only the addresses resolved then, so a later DNS change cannot move
  it.
- **Every CONNECT is authorized** against Argus: the attempt must still hold the
  task's lease (the worker status route), and the integration must still be `ready`
  at the `secret_version` and `config_version` the grant was opened for, so a grant
  for an edited server stops working even if the edit is re-validated within one
  heartbeat. Answers are `407` (no or unknown
  grant), `403` (another `host:port`, or authorization refused or failed), `405`
  (not CONNECT), `502` (the dial failed), else `200` and a byte splice. A worker's
  environment or flags cannot authorize anything: only the channel mints grants,
  from the API's status and delivery for the attempt it holds.
- **Fencing and lifetime.** Opening grants for an attempt revokes every earlier grant
  of its task, so an old attempt's grants stop working. Revocation, expiry, a
  rotation, a lost lease and the end of the attempt revoke the grants and close
  their open tunnels; a tunnel also closes after 15 minutes, and kubectl reconnects
  through a fresh authorization. Lease TTL and heartbeat are unchanged.

Before writing a kubeconfig, and again before a rotation replaces one, the channel
proves the route: a CONNECT through the listener with the grant, then a TLS
handshake only, verified against the integration's `ca_data` (the bundled public
roots when null, never the process's extra CAs) with hostname checking. No token
and no HTTP request is sent. No route configured, no bridge address, an invalid
port, an internal or unresolvable server, a refused CONNECT or a failed dial blocks
the task with `env_integration_route_unavailable`; an untrusted certificate or a
host mismatch blocks with `env_integration_gateway_intercepts`. Either leaves no
kubeconfig and no grant. Reasons name the integration and a fixed code, never the
route URL or a token.

The worker prompt says a kubectl TLS or proxy error is an environment block: never
edit the kubeconfig's server, CA or `proxy-url`, never pass `--server`,
`--certificate-authority` or `--insecure-skip-tls-verify`, never change
`HTTPS_PROXY` or `NO_PROXY`; stop and reply `FAIL:`.

**Limits of the route.** The broker is reachable from every container on the
bridge, so a grant is a capability: whoever holds the attempt's kubeconfig can open
tunnels to those clusters only, only while the attempt holds its lease. It sees only
TLS ciphertext. Workers in socket mode run sessions on the outer daemon, where the
bridge is not reachable; their Kubernetes tasks block with
`env_integration_route_unavailable`. A host firewall must let the bridge reach the
port (see the [worker setup runbook](../runbooks/2026-10-16.nanoclaw-worker-setup.ref.md)).

**MCP broker.** An MCP token never enters a session: no environment variable, file,
prompt, task text, log or artifact holds it. The channel keeps it in the memory of
its MCP broker (`argus-integrations-mcp.ts`), a plain-HTTP listener on the same
docker bridge address as the route, at `ARGUS_INTEGRATION_MCP_PORT` or a free port,
and gives the session a server URL instead:

- **Grants.** For each selected MCP integration the channel opens a grant bound to
  the task, the attempt and the integration: 32 random bytes in the path of
  `http://host.docker.internal:<port>/mcp/<grant>`. The broker keeps only its SHA-256.
  The session's MCP config (NanoClaw's `mcp_servers` for the worker's group) gets one
  server named `argus-integration-<name>` per grant, of type `http` (Streamable HTTP)
  or `sse` (legacy HTTP+SSE), whichever the probe found; the channel replaces only
  servers with that prefix. The prompt names the server and its tools, never the URL.
- **Forwarding.** A grant forwards only to its integration's URL, or, for HTTP+SSE,
  to the same-origin endpoint the server announced (the broker rewrites the
  `endpoint` event to a path under the grant). The host is resolved once at grant
  time; internal addresses are refused and only those addresses are dialled, over
  TLS verified against the public roots, never following a redirect (a `3xx`, `401`
  or `403` upstream is a `502`). The broker adds `Authorization: Bearer <token>` and
  forwards only content type, accept, protocol version, session id and last event
  id; the session's own path suffix, query, `Authorization` or other headers never
  reach the server.
- **Every request is authorized** against Argus as a route CONNECT is: the attempt
  holds the lease and the integration is `ready` at the grant's `secret_version` and
  `config_version`. An unknown or lapsed grant is `404` (never `401`, which would
  start an OAuth flow in the client).
- **Policy.** Only `initialize`, `server/discover`, `ping`, `tools/list`,
  `tools/call` and the initialized, cancelled and progress notifications pass, in
  single messages or batches; resources, prompts, completion, logging and every
  other method get a JSON-RPC error (`-32003`, "denied by Argus policy"). Server
  requests other than `ping` (sampling, elicitation, roots) are dropped, and the
  capabilities a server advertises are cut to `tools`. `tools/list` answers keep
  only allowlisted tools (`allowlist`) or every tool (`all`); `tools/call` must name
  a tool the broker has seen listed and allowed. Annotations are passed through
  unread.
- **Pinning.** The first `tools/list` answer of a grant pins each tool's name,
  title, description and schemas and each page's set of names. A later answer that
  differs blocks the grant for the rest of the attempt: that request gets the
  error, every other open request and stream is cut, and later requests are `404`;
  the channel logs the integration and reason. A rotation or edit opens a new grant
  under the same URL and pins afresh. Annotation changes do not block.
- **Lifetime.** A grant lives 15 minutes from its last renewal, never past the
  credential's `expires_at`; each heartbeat recheck that keeps the integration
  renews it, so a channel that stops rechecking loses MCP access within that time.
  Opening grants for an attempt drops every other attempt's grants of the task.
  Revocation, expiry, an edit or rotation that is not ready, a lost lease, the end of
  the attempt and a restart of the channel drop the grants and remove the session's
  `argus-integration-*` servers. A rotation or a ready edit keeps the URL and
  transport and sends the new token from the next request; a server that changes
  transport fails closed. A response stream closes after an hour at most.
- **In flight.** Dropping a grant cuts its open requests and streams at once; the
  session sees a closed connection. A call already forwarded may still complete at
  the server; its answer is not relayed.

Before granting, the broker probes each server with the token: modern
`server/discover`, else legacy `initialize` (whose session it deletes), else an
HTTP+SSE `GET` that must announce a same-origin endpoint. A `401`/`403`, a redirect
or neither transport blocks with `env_integration_unavailable`; no bridge, a bad
port, an unresolvable or internal host, or a refused or timed-out connection with
`env_integration_route_unavailable`; an untrusted certificate with
`env_integration_gateway_intercepts`. Either leaves no grant and no session server.

The worker prompt lists each server and its allowed tools (`mcp__<server>__<tool>`)
and says a policy denial is final: do not retry it or reach the server another way;
if the tools are missing or every call fails, stop and reply `FAIL:`.

**Limits of the broker.** A grant URL is a capability for its attempt: whoever on
the bridge holds it can call the allowed tools while the attempt holds its lease. It
is stored in NanoClaw's container config for the group while the attempt runs.
What an allowed tool does is the server's; the broker cannot tell a read from a
write, so `read_only` rests on the admin's allowlist (3.2). Revoking in Argus does
not revoke the token at the server.

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

Active rows offer **Validate**, **Edit**, **Rotate** (new token) and **Revoke**
(confirm by typing the name). **Edit** changes the target (MCP URL; Kubernetes server,
namespace, CA bundle), the intended access and, for MCP, the tool allowlist (one name
per line, empty for none) at the record's `version`, with no token field; it says
that saving resets validation. A stale version, an invalid config and a revoked
record get fixed messages. MCP rows show their tool policy: the allowlist size,
"all tools", or "unknown coverage: unusable by workers". Pending actions are
disabled. Revoked rows remain visible without these actions. Each row shows its
expiry; the add and rotate forms take an optional expiry in local time. The screen
shows secret-store readiness and explains that connections reach only attempts of
tasks that select them (Claude engines): a Kubernetes one as a kubeconfig, an MCP
one as a broker URL that adds the token and passes only allowed tools, so the
session never sees the token; that removing access in Argus does not revoke the
token; and that OAuth is unavailable. It links to Accounts, Settings and worker pools without
claiming that pools grant tasks access to these connections.

A task's detail view lists the project's active integrations of both kinds (and any
already selected) as checkboxes and saves the selection at the task's `version`. It
is read-only while an attempt runs or after the task ends; errors are fixed messages.
Each choice shows its scope (the Kubernetes namespace, or the MCP tools, "all tools"
or none), access and expiry, or why an attempt would block (revoked, expired, not
validated, read only without a tool allowlist). The New task form offers the
project's active integrations, none chosen by default, and sends the choice as
`integrations`.

# 4. Open Questions

- Should the orchestrator see integration names (not secrets) to plan tasks?

## Open items

- MCP OAuth sign-in (3.2).
- Kubernetes and MCP delivery for engines other than Claude (3.6 engine matrix).
- Kubernetes and MCP routing for socket-mode workers (3.6 Route, MCP broker).
- Verify the route live on a worker against a real cluster, after the operator
  enables it there.
- Verify the MCP broker live on a worker, after the operator enables it: that the
  session's MCP client reaches the grant URL directly rather than through the OneCLI
  gateway (`HTTP_PROXY`/`NO_PROXY`; through the gateway it would see the grant URL,
  not the token), that NanoClaw applies `mcp_servers` to a session spawned after
  preflight, and whether a Claude executor's delegated run sees those servers.
- Verify authentication and tool scope live against the MCP server an operator
  first connects (for example `https://agent-gateway.radar2m.com/mcp`): neither has
  been tested, and Argus claims no particular upstream scope for it (in particular
  not `radar:cluster-read`).

# 5. Constraints

- Single node: SQLite plus the SOPS file; no new service.
- Writes through `Database.write()` with `tx.now`; network checks run outside it.
- The dashboard talks to the API only.

# 6. Implementation Consequences

- Migrations `0080_integrations.sql`, `0081_task_integrations.sql` (the
  `task_integrations` table and `integrations.expires_at`) and
  `0082_integration_config_version.sql`; package
  `argus_api/integrations` (service, validation, `selection`, `delivery`, routes);
  `make openapi`.
- Dashboard: `views/Integrations.tsx`, the Setup tab in `App.tsx`,
  `views/TaskIntegrations.tsx` in the task detail.
- NanoClaw: `argus-integrations.ts` (install, recheck, cleanup, prompt hint) and
  `argus-integrations-route.ts` (the Kubernetes route broker and probe) and
  `argus-integrations-mcp.ts` (the MCP broker and probe), wired in `argus.ts` with
  the session's `mcp_servers`; `engines/argus-delegate.mjs` scopes `KUBECONFIG`;
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
  consistency. Edits cover the kept credential and id, `stale_version`, revoked and
  foreign records, create's config checks with no upstream request, the validation
  reset, the audit diff, an edit voiding an in-flight validation, the MCP tool
  policy, and no secret in responses, events, logs or the database. All upstream
  traffic uses a fake transport and resolver.
- Focused dashboard checks: `pnpm --filter @argus/dashboard exec vitest run
  src/views/Integrations.test.tsx src/views/TaskIntegrations.test.tsx
  src/App.test.tsx`, dashboard TypeScript checking,
  and Biome on the touched dashboard files. Canonical full gates belong to the parent
  run.
- Component coverage uses a fake fetch transport through the generated client and
  real live-data loading: both forms, project isolation, loading/empty/error states,
  token clearing on success and failure, response/error non-disclosure, validation,
  rotation, editing (payload, version, tool policy, fixed errors), revocation
  confirmation and unavailable actions on revoked records.
- Delivery: `uv run pytest apps/api/tests/test_integration_delivery.py` covers
  selection validation (MCP included; unknown, foreign, revoked and `unknown`-policy
  ids refused) and versioning, the lease binding, the engine matrix, every readiness
  refusal (`tools_unknown` included), the MCP credential shape, the version race,
  config-edit fencing and the secret-free audit event.
- MCP broker: vitest `argus-integrations-mcp.test.ts` runs the broker against fake
  HTTPS MCP servers on loopback (a test CA, an impostor CA, a fake token) over
  Streamable HTTP with JSON and SSE answers and legacy HTTP+SSE: discovery, list
  filtering, allowed and denied calls (batches included), denied methods, dropped
  server requests, pinning blocks for a changed schema or tool set, ignored
  annotation changes, path, query, method and header widening, redirects and `401`
  as `502`, probe failures and their reasons, authorization per request,
  revocation cutting a stream, attempt fencing, rotation, expiry and TTL renewal,
  and no token in anything the session receives or in the environment.
- Channel: vitest `argus-integrations.test.ts` (paths, permissions, symlinks, atomic
  writes, rendering with a `proxy-url` per cluster, recheck, cleanup, grant
  authorization per attempt, secret and config version, re-granting after a
  config edit, and the broker over real TLS: splice with
  ciphertext only, `407`/`405`/`403`, refused authorization, DNS pinning, internal
  addresses, revocation closing tunnels, attempt fencing, tunnel lifetime, CA and
  hostname mismatch, no bridge, bad port, cluster down) and
  `argus-integrations-channel.test.ts` (preflight blocks for both route reasons and a
  missing route or MCP broker, re-granting on rotation, revocation on completion,
  revocation and lease loss, cleanup on every path, delegate passthrough, the MCP
  server in the session config with neither token nor URL in the prompt or logs),
  plus
  `argus-delegate.test.mjs` and `tests/test_command_guard.py`.
- End to end: `ARGUS_TEST_KUBECTL=<kubectl> uv run pytest
  apps/api/tests/test_integration_kubectl_e2e.py` runs the API, the channel module and
  a real kubectl against a local fake Kubernetes API over TLS with a test CA and a
  fake token: allowed reads in the namespace, forbidden other namespaces and writes,
  a request without the token rejected, cross-project refusal, expiry and revocation
  removing the file. Through an intercepting gateway fixture (another CA) kubectl
  fails with x509 and the gateway never reads the bearer. Through the channel's
  broker, authorized by the real API, kubectl succeeds while a relay in front of the
  cluster sees ciphertext only; a `--server` override, a stale attempt, revocation,
  a CA mismatch and a hostname mismatch are refused. Skipped without a kubectl.
- No real MCP server, MCP client, Kubernetes cluster, credential or model run is
  exercised by these checks.
