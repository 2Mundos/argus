# Global account selection

# 1. Purpose and Scope

Which provider account a worker leases is decided **globally, by priority and
actual availability**, never by a provider list configured on a pool or a worker.
Scope: the lease and exhaustion-fallback ranking in `apps/api`, the pool/worker
configuration that used to carry provider lists, the engine router, and the
NanoClaw channel (engine and executor leases, model policy, `argus-delegate`).
Builds on [worker lifecycle 3.9](worker-lifecycle.spec.md); supersedes its
provider-order wording and the Codex-primary lease order of
[Codex accounts](codex-accounts.spec.md).

# 2. Non-Goals

No new quota model, no priority editing UI, no provider-specific scoring, no
preemption of running work, and no change to credential storage, OneCLI secret
isolation or sandbox/TLS restrictions.

# 3. Decisions Made

- **Custom accounts rank like any other.** A `custom` account
  ([custom accounts](custom-accounts.spec.md)) is leased for `engine` or `executor`
  when the runtime lists `custom` in `runtime_providers`; the lease carries `settings`
  for the role. An HTTP 402/429 from its endpoint is quota (same failover path, no
  attempt consumed).

- **One global ranking.** A lease (`POST /api/accounts/lease`, MCP
  `lease_account`) considers every usable model account and picks by
  `priority` descending, then fewest active leases, then provider, name and id,
  so ties are deterministic. Usable means: not `disabled`, not `exhausted`
  before its reset (an exhausted account past its reset counts and is recovered
  on lease), a credential present in the secret store (a missing one disables
  the account and the next candidate is tried), and below `max_leases`.
  Cloud accounts rank before the `local` one, except for a `trivial` task, which
  prefers `local` and falls back to the cloud in the same call
  ([task complexity](task-complexity.spec.md)).
  Slot accounting stays atomic in the one write transaction.
- **Eligibility is technical, never preference.** `gitea` accounts serve only the
  `repository` role (scoped by the pool's `repositories`) and never become model
  accounts; executor-only providers (`cursor`) cannot be a harness (`engine`
  role). The lease request may carry `runtime_providers`: what the leasing
  runtime can technically apply (for NanoClaw, the providers with a vault
  mapping; engines exclude executor-only ones). It is required and non-empty
  for credential delivery, and narrows eligibility, never orders it. The old
  `providers` field is accepted and ignored. Every credential-bearing lease is
  bound to `task_id` and `attempt` and requires that task to be currently claimed
  by the caller with an unexpired lease. NanoClaw claims first, then leases and
  applies the engine credential; it does not fetch credentials while idle. An
  operator release of the task ([worker lifecycle](worker-lifecycle.spec.md) 3.3)
  ends the holder's account leases (`task_released`) unless it holds another
  claim, and the released pair fails every one of these checks with `409`.
- **Exhaustion and recovery use the same ranking.** After
  `POST /accounts/{id}/exhausted` the remaining usable accounts are ranked
  globally; the previous provider gets no preference. The response contains
  next-account metadata only, never a credential; the worker obtains the secret
  through the task-bound lease endpoint. A recovered or newly added
  higher-priority account is considered by the **next lease**; a held lease is
  never preempted. The NanoClaw channel re-evaluates accounts only for a claimed
  task (at most once a minute) and rotates (OneCLI secret, harness, stopped
  containers) only when Argus returns a different account.
- **Pool and worker provider lists are retired.** `nanoclaw.account_providers`,
  `nanoclaw.executor_providers`, `ARGUS_NANOCLAW_ACCOUNT_PROVIDERS`,
  `ARGUS_NANOCLAW_EXECUTOR_PROVIDERS`, `ARGUS_ACCOUNT_PROVIDERS` and
  `ARGUS_EXECUTOR_PROVIDERS` no longer exist in the schema, REST/MCP or the
  provisioning scripts. Persisted pools that still carry the keys have them
  dropped when read; a worker that still has the env set logs a warning and
  ignores the values (the legacy `ARGUS_ACCOUNT_PROVIDERS` only counts as
  "account leasing on"). Account leasing in NanoClaw is on whenever `ONECLI_URL`
  is set, which the fleet always does.
- **Engine, executor and model policy follow the leased account.** The channel
  selects the harness from the engine account's provider. The executor lease is
  the best usable account for a delegated CLI across all runtime-supported
  providers; it is held only when its provider differs from the engine's (the
  vault keeps one account per provider). The channel asks with `reuse_held`: if
  the best executor is the account the worker already holds as its engine (even
  at `max_leases`), Argus returns that engine lease unchanged, no second row and
  no extra slot, so a lower-priority executor (say Codex 0 beside a full Claude
  100) never displaces the engine-backed CLI. Proven contract: capacity counts
  rows, a separate executor lease of the same account still takes its own slot
  (the default without `reuse_held`). The task text names the preferred
  `EXECUTOR` (claude | codex | agent) of the best leased account and
  `argus-delegate` reports `preferred_executor` and `matches_preferred`; it never
  switches executor itself. The model-policy snapshot pinned for
  each task carries `engine`, `account_provider` and `executor_account_provider`
  (no account secret), `argus-delegate` reports them, and a policy without a
  harness chain for the selected engine fails the task instead of keeping the
  previous engine's model.
- **Engine router.** `POST /engines/route` and the local-run escalation target
  choose the usable harness engine whose best **leasable** account has the
  highest priority (ties: engine id). `AccountsSummary` keeps the lifecycle
  counts (`available`, `exhausted`, `disabled`) and adds `busy` (available but at
  `max_leases`), `no_credential` (available but the secret is missing; computed
  read-only, nothing repaired or returned), `leasable` and `best_priority` over
  the leasable ones. A full top-priority account therefore does not win a route
  that the real lease would give a free lower one; releasing a slot restores it.
  Codex is no longer primary and Claude no longer the fixed executor; the
  executor is advisory and named only when it differs from the engine.

- **Claude subscription auth across Claude Code's tool boundary.** Claude Code
  removes `CLAUDE_CODE_OAUTH_TOKEN` from the environment of every process its
  Bash tool starts (verified on 2.1.296 against a local mock API: the CLI itself
  authenticates with it, its tool subprocess sees it `absent`, while
  `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`, proxy and CA variables stay). So
  `argus-delegate claude` run from the primary's Bash tool saw no credential and
  exited 78 even though the gateway serves the account. Fix, with no real token
  anywhere in the worker: the trusted host, which holds OneCLI's container config
  for the agent group, writes a read-only stub
  `/home/node/.config/argus/claude-gateway.json` (`version`, `provider`,
  `variable`, `sentinel`, `proxy` as `host:port` without credentials, `ca`) **only
  when OneCLI issued a `CLAUDE_CODE_OAUTH_TOKEN` sentinel for that group** (an
  Anthropic account is assigned), the value passes the sentinel rule (opaque, no
  whitespace, ≤256 chars; a `sk-ant-…` or >120-char value must name itself a
  placeholder), a proxy and CA path are present and the API is not redirected
  (`ANTHROPIC_BASE_URL`/`ANTHROPIC_UNIX_SOCKET`). `argus-delegate claude`, only
  when it has no credential variable of its own, restores the variable for its
  `claude` child after checking the stub (regular, small, not other-writable,
  exact keys), the sentinel rule, that its `HTTPS_PROXY` is the recorded gateway
  address, and that a CA file exists; any doubt leaves the environment as is and
  the run fails `auth_missing` with a `gateway_stub` reason code. `authStatus`
  stays strict, the stub path is fixed, nothing else is exported, the report gets
  `auth_source: gateway_sentinel`, and no executor, model, sandbox or TLS setting
  changes. No assignment, no stub.

- **Credential rotation.** An agent may update a credential only while it holds
  that account lease **and** the live claim named by the `task_id` and `attempt`
  in the request (both required for agents; checked like a lease: claimed by the
  caller, current attempt, unexpired). A lease that outlived its claim cannot
  write. Missing `task_id`/`attempt` is `422`; a stale, foreign or expired claim
  is `409`; no lease on the account is `403`. The new document must keep the
  provider credential kind and nested document structure, and a `gitea` rotation
  must keep its `username` (`422` otherwise). The stored secret is untouched on
  any refusal. The claim is checked before the secret store write, not inside
  one transaction (the store does I/O), so a claim that ends in that short
  window can still land one write for the credential the agent already held. Admin renewal remains the
  operator path for changing kind or identity and needs neither.
- **Quota failover (3.6).** A provider quota never costs an attempt.
  - *Server path.* A task `FAIL` with reason `provider_failure` whose error is a
    quota notice for the engine lease's provider (the one pattern set in
    `argus_core.quota`: Claude `Rate limit …` / `Out of credits`, Codex
    `usage_limit_reached`, `insufficient_quota`, usage-limit sentences) is not a
    failure: Argus marks the holder's engine account exhausted (until the parsed
    reset, else the cooldown; an existing later mark is never shortened),
    releases the lease, leases the next usable account, and puts the task back to
    `ready` on the **same attempt** with `failures` unchanged. With no account
    left it becomes `blocked` / `awaiting_quota`. A claim still bumps `attempt`
    (that is how stale writers are fenced), but the attempt is "not consumed":
    `max_attempts` counts `failures`, which a quota pause never touches. It is
    fenced on owner and attempt (an expired lease is accepted, as for `fail`); a
    repeat or a stale report is refused (409) and changes nothing, so it is
    idempotent. The same fence now guards `POST /accounts/{id}/exhausted` with
    `task_id`/`attempt`. The pattern set is mirrored in
    `integrations/nanoclaw/argus-accounts.ts` and both are tested against
    `packages/core/tests/fixtures/quota-notices.json`.
  - *Awaiting quota.* `blocked` with `block_reason=awaiting_quota` replaces
    `no_account` (rows with the old value are still read and released the same
    way; no migration). `Task.refill_at` is the earliest `exhausted_until` of the
    accounts that would serve it, derived on read. `recover()` (reaper), a new or
    re-enabled account and a secrets reload return these tasks to `ready`. A
    `ready` task nobody can lease for is also marked: the channel calls
    `POST /accounts/awaiting-quota` (capabilities, runtime providers) when its
    lease fails with `no_account`; the endpoint refuses (409 `account_available`)
    while any account is usable, and only blocks tasks that worker could claim.
  - *Channel.* When a run ends, the channel treats as exhaustion: a quota notice,
    a structured `providerFailure` `{provider:"claude", kind:"rate_limit",
    resets_at?}` (anthropic account; fixed reason, never raw text), or the generic
    "The agent run failed" notice plus a `Rate limit [..] (resets ..)` or
    `You've hit your session|usage|weekly limit · resets ..` line in the agent log
    tail. The log is read through the optional `agentLogTail` hook of the channel
    config; the NanoClaw host does not provide it (the channel has no handle on
    the agent's log, and the runner already emits the structured field), so it
    gets the notice and structured cases.
  - *Runner.* The NanoClaw agent runner, as `install.sh` patches it, turns a
    failed Claude turn whose result is exactly one
    `^You('|’)ve hit your (session|usage|weekly) limit · resets .+$` line, or
    that the SDK marked `rate_limit`, into that structured `providerFailure`
    (`resets_at` only for a UTC clock time) with the generic notice text; no
    other upstream text reaches the channel. The patch asserts its anchors in
    NanoClaw's code, so upstream drift stops the install. On exhaustion it reports the account,
    leases the next one, recycles the runtime and resends the task on the same
    attempt, and it does not report a failure. If Argus itself recognised a
    quota notice on `/fail` and switched the account, the channel drops its stale
    account and recycles when it re-leases.
  - *Dynamic priority.* Exhausted accounts are out of the ranking until
    `exhausted_until`; an `exhausted` account past its reset is recovered on lease,
    never demoted. There is no priority editing.
  - *Visibility.* Every switch logs `account_failover {from,to,reason,task_id,
    attempt}` on the API and the channel and records an `account.failover` event
    (subject: the task; data `from`, `to`, `reason`, `attempt`). The Task view
    adds `serving_account` (id, name, provider), `failover` (`from_account`,
    `to_account`, `reason`, `at`, `attempt`) and `refill_at`, all derived from
    accounts, leases and events; the dashboard task detail shows them with the
    literal reason. Names only, never credentials.
  - *Exclusivity.* Unchanged and tested under concurrency: one lease per agent
    and role, a task is claimed by one agent, and `max_leases` holds.
- **Who reports exhaustion.** `POST /accounts/{id}/exhausted` takes the **agent
  token of the engine that holds the lease**: the NanoClaw channel runs in the
  host, registered as an agent, and reports with its own token (it also matches
  the lease holder, `not_leased` otherwise). Admin tokens get 403 and an unknown,
  revoked or ended agent's token gets 401 by design; a channel whose agent was
  ended (lost, left, re-registered) must re-register for a fresh token. Nothing in `authenticate` expires a live
  agent's token, so a 401 here means a stale registration, not a bug.
- **Claude's own wording counts.** The channel and the API (`quota.py`, same
  shared fixture) recognise, for `anthropic` accounts, `Rate limit […]`,
  `Out of credits`, `Usage limit reached` and `You've hit your [session|usage|weekly ]limit`
  (straight or curly apostrophe), and the channel a structured
  `{provider:"claude", kind:"rate_limit", resets_at?}`. The reset is
  `(resets <ISO>)` or `resets 2am (UTC)` / `resets 11:50pm (UTC)` (the next such
  UTC clock time, strictly after now: the transaction time on the API, an
  explicit `now` in the fixture); unparsable means no `until` and the default
  cooldown. Structured reports carry a fixed reason, never log text.
- **Claims wait for quota.** `POST /tasks/claim` returns nothing (no attempt
  consumed, the task stays `ready`) while at least one account is `exhausted`
  and none is `available`. Fleets with no accounts, or with any available
  account, are unaffected. When `recover()` returns accounts to `available`
  after `exhausted_until`, claims work again. The overview reports this as
  `accounts.awaiting_quota`, with the refill time in `accounts.next_reset_at`.

# 4. Open Questions

Priority is operator-set and static; there is no quota-aware weighting. Whether
a rate-limited-but-not-exhausted account should rank lower is not decided.

# 5. Constraints

Single-node spec applies: no new service, no migration (3.6 derives its Task
fields from accounts, leases and events; the lease row's
`providers` column is kept and now records the runtime capability list). No
network calls inside a write transaction. Credentials are returned only in the
lease response, are written only to the OneCLI vault, and never to logs, events,
the model-policy snapshot or `argus-delegate` output.

# 6. Implementation Consequences

- `packages/core`: `accounts.py` (`role_accepts`, `LeaseRequest.runtime_providers`,
  `ExhaustedReport.runtime_providers`), `fleet.py` (retired options dropped),
  `engines.py` (`best_priority`).
- `apps/api`: `accounts/service.py` (`_try_lease`, `exhausted`, `earliest_reset`),
  `engines/router.py` and `routes.py`, `config.py`, `fleet/runtimes.py`,
  `fleet/templates.py`, `mcp_server.py`; OpenAPI and client types regenerated.
- Rollout of the Claude sentinel stub needs two layers: the NanoClaw **host**
  (`install.sh` adds `argus-claude-stub.ts` and wraps `onecli.ts`; rebuild and
  restart the host) and the **agent image** (`argus-delegate.mjs`, via
  `engines/install-engines.sh` and any project layer built on it). Running
  containers keep their old mounts and delegate: recreate them.
- `integrations/nanoclaw`: `argus.ts` (global leases, idle rebalance, held
  accounts to the model policy), `argus-accounts.ts` (`runtimeProviders`),
  `engines/` (policy context, delegate identity), `provision.sh`, README. Recreate
  worker instances (and re-run `install.sh`) to pick the change up; no deployment
  is part of this change.
- Quota failover (3.6): `packages/core` `quota.py` (patterns), `tasks.py`
  (`Action.REQUEUE`, `ready → blocked`, `refill_at`, `serving_account`,
  `failover`), `accounts.py` (`AwaitingQuotaRequest/Result`); `apps/api`
  `accounts/service.py` (`fail_task`, `exhausted`, `mark_awaiting_quota`),
  `tasks/service.py` (`pause_for_quota`, `await_quota_ready`), `tasks/routes.py`,
  `accounts/routes.py`; dashboard `failover.tsx`; OpenAPI and client regenerated.
- Rollout: the channel change (`argus.ts`, `argus-accounts.ts`) needs the NanoClaw
  **host rebuilt and restarted** (it loads the channel) and Argus upgraded first
  (the channel calls `/accounts/awaiting-quota`; an older Argus answers 404, which
  the channel logs and ignores). Provide `agentLogTail` from the host to enable the
  log-derived case.
- Claude quota block in the runner: `integrations/nanoclaw/overlays/claude-rate-limit.ts`
  and `patch-claude-rate-limit.py` (run by `install.sh`). It lives in the agent
  image: rerun `provision.sh` (README, "Updating an installed copy") and recreate
  the worker instances.
