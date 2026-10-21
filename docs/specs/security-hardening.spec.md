# Security hardening (2026-10-20 review, medium/low)

# 1. Purpose and Scope

Close the medium and low findings from the 2026-10-20 security review that are
safe to ship without a larger auth redesign. High findings (readyz write,
worker-scoped GETs, lease hardening, and related “quick wins”) stay on their
own tasks.

In scope:

- HTTP response headers and dashboard CSP; API docs off by default
- Dashboard bearer token in `sessionStorage` (migrate from `localStorage`)
- Markdown without raw HTML (`rehype-raw` stays unused)
- Kubernetes pod and Docker container security context; mutable-image pull policy
- Docker host URL scheme allowlist; OneCLI URL and error-body rules
- Task `spec` / `input` size caps; per-agent artifact byte quota
- Admin token file mode; NanoClaw host entrypoint path handling
- MCP DNS-rebinding Host/Origin allowlist
- Absolute agent-token expiry with heartbeat sliding renewal

Related: [single-node](single-node.spec.md), [worker lifecycle](worker-lifecycle.spec.md),
[prompt-data isolation](prompt-data-isolation.spec.md), [MCP operations](mcp-operations.spec.md).

# 2. Non-Goals

- General request rate limiting (needs its own spec: where limits live, how they
  interact with SSE/MCP long polls, and operator knobs).
- Pool `env` encryption at rest (SOPS / secret refs): needs a migration path for
  existing pools and a dedicated secrets-in-pool spec.
- Bootstrap token delivery via file/socket instead of container env: needs
  host-side changes and a NanoClaw release.
- Moving the admin token from env to file-only, or changing admin token entropy:
  part of a larger auth spec.
- Token rotation when two clients share one agent identity.
- High-priority items already owned by other tasks (`/readyz` write probes,
  worker-scoped GETs, lease hardening). Trivial fixes in files already touched
  (e.g. not logging the admin token value) may land here; no auth restructure.

# 3. Decisions Made

1. **Response headers.** Every response gets `X-Content-Type-Options: nosniff`,
   `X-Frame-Options: DENY`, and `Content-Security-Policy: frame-ancestors 'none'`.
   Dashboard HTML (`text/html`) gets a strict CSP:
   `default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline';
   script-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none';
   base-uri 'none'`. No permissive CORS middleware.
2. **API docs off by default.** FastAPI `docs_url`, `redoc_url`, and `openapi_url`
   are `None` unless `ARGUS_API_DOCS=1`. `scripts/export_openapi.py` sets that
   flag before importing the app so `make openapi` / `make openapi-check` keep
   working; the committed schema is unchanged unless the API surface changes.
3. **Dashboard token storage.** The bearer token key stays `argus.token`. Reads
   prefer `sessionStorage`; a one-time migration copies from `localStorage` then
   removes it. Writes go only to `sessionStorage`.
4. **Markdown.** `Markdown.tsx` must not enable `rehype-raw`. Raw HTML in message
   bodies renders as text.
5. **Pod security.** `pod_manifest` sets pod-level `securityContext` with
   `runAsNonRoot: true` and `seccompProfile: {type: RuntimeDefault}`, and container
   `runAsNonRoot`, `readOnlyRootFilesystem` (from the spec),
   `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`.
6. **Docker run hardening.** Every `containers.run` keeps `cap_drop=["ALL"]`,
   `security_opt=["no-new-privileges:true"]`, and `read_only` when the spec says
   so. Extra capabilities (e.g. remote mkdir/chown helpers) stay explicit.
7. **Mutable images.** An image reference without `@sha256:` is mutable. If it is
   already present locally, Argus uses it and never re-pulls. If it is missing,
   start is refused unless the pool sets `allow_mutable_images: true` (default
   false). Digested references may still be pulled when absent.
8. **Docker host schemes.** `DockerOptions.host` accepts `unix://`, `ssh://`, and
   `tcp://` only with TLS (`?tls=true` or equivalent). Plain `tcp://` is rejected.
   An `https://` form is accepted.
9. **OneCLI URL.** `NanoclawOptions.onecli_url` and the NanoClaw `OneCliClient`
   accept `https://`, or `http://` only for loopback / `host.docker.internal`
   hosts. Userinfo in the URL is rejected. Default
   `http://host.docker.internal:10254` remains valid.
10. **OneCLI errors.** Client errors expose status and a short code only. Response
    body text is included only when `ARGUS_ONECLI_DEBUG_BODY=1`.
11. **Task size caps.** `TaskCreate.spec` max 200_000 characters; `TaskCreate.input`
    max 100_000 characters of canonical JSON (same order of magnitude as knowledge
    body). `TaskChanges.spec` shares the spec cap.
12. **Artifact quota.** `ARGUS_ARTIFACT_AGENT_QUOTA_BYTES` (default 2 GiB) caps the
    sum of artifact sizes created by one agent. Exceeding it returns a domain
    error with code `artifact_quota`.
13. **Admin token file.** `ensure_admin_token` opens with
    `O_WRONLY|O_CREAT|O_TRUNC|O_NOFOLLOW` and mode `0o600`, then `fchmod` to
    `0o600` before writing. No post-write `chmod`.
14. **Entrypoint version.** `deploy/nanoclaw-host/entrypoint.sh` reads
    `package.json` version via `node -e` with the path as `argv`, after validating
    the install path (absolute, no quotes/newlines).
15. **MCP DNS rebinding.** Streamable HTTP enables
    `enable_dns_rebinding_protection` with `allowed_hosts` / `allowed_origins`
    derived from localhost names plus the host (and port) of `ARGUS_PUBLIC_URL`,
    and `host.docker.internal` when that public URL is local-facing.
16. **Agent token TTL.** `ARGUS_AGENT_TOKEN_TTL_S` (default 7 days) sets
    `tokens.expires_at` at agent registration. Auth rejects expired agent tokens
    with `401` / `token_expired`. Heartbeat slides `expires_at` forward by a full
    TTL when less than half the TTL remains. Bootstrap tokens keep their own TTL.
    Existing agent rows with `expires_at IS NULL` stay valid (grandfathered).
    Column `tokens.expires_at` already exists (migration 0003/0004); no new
    migration is required.

# 4. Open Questions

- Whether grandfathered NULL `expires_at` agent tokens should later get a
  one-shot backfill TTL on first heartbeat after upgrade.
- Whether `allow_mutable_images` should also gate Kubernetes `imagePullPolicy`.

# 5. Constraints

- Single-node: no new required services for quotas or rate limits.
- Negentropy: no general rate-limit middleware in this change.
- OpenAPI drift: regenerate only when the public schema changes
  (`allow_mutable_images`, field max lengths, etc.).
- Do not break `http://host.docker.internal:10254` as the default OneCLI URL.

# 6. Implementation Consequences

- Spec and `docs/README.md` link land first; each finding is its own
  `fix(security): …` commit with a negative test.
- Dashboard vitest covers sessionStorage migration and Markdown HTML escaping.
- Fleet/core tests cover Docker host schemes, image pull refusal, OneCLI URL
  accept/reject, task size validation, artifact quota, admin-token mode,
  entrypoint path handling, MCP Host rejection, and agent token expiry/renewal.
- Deferred items (rate limiting, pool env encryption, bootstrap-via-file, admin
  token entropy/env policy, high quick-wins) are listed in the PR body, not
  half-implemented.
