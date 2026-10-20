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

- **One global ranking.** A lease (`POST /api/accounts/lease`, MCP
  `lease_account`) considers every usable model account and picks by
  `priority` descending, then fewest active leases, then provider, name and id,
  so ties are deterministic. Usable means: not `disabled`, not `exhausted`
  before its reset (an exhausted account past its reset counts and is recovered
  on lease), a credential present in the secret store (a missing one disables
  the account and the next candidate is tried), and below `max_leases`.
  Slot accounting stays atomic in the one write transaction.
- **Eligibility is technical, never preference.** `gitea` accounts serve only the
  `repository` role (scoped by the pool's `repositories`) and never become model
  accounts; executor-only providers (`cursor`) cannot be a harness (`engine`
  role). The lease request may carry `runtime_providers`: what the leasing
  runtime can technically apply (for NanoClaw, the providers with a vault
  mapping; engines exclude executor-only ones). It narrows eligibility, never
  orders it. The old `providers` field is accepted and ignored.
- **Exhaustion and recovery use the same ranking.** After
  `POST /accounts/{id}/exhausted` the remaining usable accounts are ranked
  globally; the previous provider gets no preference. A recovered or newly added
  higher-priority account is considered by the **next lease**; a held lease is
  never preempted. The NanoClaw channel re-leases only while idle (no task in
  flight), at most every 60 s, and rotates (OneCLI secret, harness, stopped
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

# 4. Open Questions

Priority is operator-set and static; there is no quota-aware weighting. Whether
a rate-limited-but-not-exhausted account should rank lower is not decided.

# 5. Constraints

Single-node spec applies: no new service, no migration (the lease row's
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
- `integrations/nanoclaw`: `argus.ts` (global leases, idle rebalance, held
  accounts to the model policy), `argus-accounts.ts` (`runtimeProviders`),
  `engines/` (policy context, delegate identity), `provision.sh`, README. Recreate
  worker instances (and re-run `install.sh`) to pick the change up; no deployment
  is part of this change.
