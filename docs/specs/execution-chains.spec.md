---
status: implemented
created: 2026-11-17
updated: 2026-11-27
---

# Execution chains

# 1. Purpose and Scope

For **each** difficulty (`trivial`, `simple`, `standard`, `hard`), configure an
ordered fallback list of **complete execution alternatives**. An alternative is
one harness/executor pair or one single-layer model doing the work itself. Both
kinds may appear in the same list. The resolver selects whole alternatives,
never the Cartesian product of independent harness and executor rankings.

This is the owning contract for replacing the legacy routing described in
[task difficulty](task-difficulty.spec.md), [settings](settings.spec.md) and
[task models](task-models.spec.md). The opt-in policy now runs through settings,
atomic claim admission, durable traversal, NanoClaw model application and the
Setup editor. Unconfigured difficulties retain the legacy path unchanged.
No existing account, capacity, model server or live setting is migrated.

Scope: typed policy, global/project inheritance, capability validation, account
reservation, claim fencing, persisted fallback, REST/MCP, Setup, diagnostics,
NanoClaw prompts and model selection. A local direct route is one instance of
this model, not a special difficulty rule. A paid fallback exists only if the
operator explicitly includes an alternative that uses it.

# 2. Non-Goals

- No account creation, priority or capacity changes, model server replacement,
  inference probe, spend approval, deployment or automatic settings migration.
- No arbitrary command, URL, credential, environment or gateway grant in a route.
- No new worker runtime or conversion of an executor-only CLI into a harness.
- No enabling or extending the [single-layer experiment](single-layer-experiment.spec.md).
- No changes to legacy task-pin behavior when routing through the legacy path.
- No implicit cheapest-model optimization or provider-ranked fallback outside a
  configured alternative list; no per-layer fallback inside an alternative.

# 3. Decisions Made

## 3.1 One setting and one discriminated contract

Add `execution.chains`, admin-write/readable under existing settings permissions,
with scopes `global` and `project`. The value is:

```json
{
  "version": 1,
  "difficulties": {
    "trivial": [
      {
        "id": "direct-local",
        "mode": "single",
        "runtime": "nanoclaw",
        "harness": {
          "adapter": "claude",
          "provider": "local",
          "account_id": "example-local-account",
          "model": {"source": "account"}
        }
      },
      {
        "id": "local-with-opencode",
        "mode": "paired",
        "runtime": "nanoclaw",
        "harness": {
          "adapter": "claude",
          "provider": "local",
          "account_id": "example-local-account",
          "model": {"source": "account"}
        },
        "executor": {
          "adapter": "opencode",
          "provider": "custom",
          "account_id": "example-custom-account",
          "model": {"source": "explicit", "id": "vendor/coding-model"}
        }
      }
    ]
  }
}
```

The account IDs and model above are illustrative, not production values or
recommendations. `simple`, `standard` and `hard` take exactly the same lists and
selectors, including a single-layer first choice and mixed fallback modes.

- A selector names its exact provider, an adapter, and a model source, and may
  pin **one existing account ID**. `account_id` is optional: omitted (or null) means
  "any available account of this provider that can serve the role", and admission
  picks the best usable one (priority, then least leased) inside the same
  transaction and records it. A pin forces exactly that account and never falls
  back. Account names can be displayed, but the wire and persistence use IDs. No
  `auto` provider, model chain or native default is allowed in an alternative, and
  an unpinned selector never crosses providers or roles.
- Model is a closed union: `{source: explicit, id: <model-id>}` or
  `{source: account}`. Explicit IDs use the existing opaque model-ID validator.
  The latter means that account's configured model, or its own known discovered
  served model where local account semantics already allow that. If neither is
  known at admission, the alternative cannot run; never substitute a policy model.
- For a local harness or executor, use `source: account`; do not introduce a
  second model setting that disagrees with the local account. For a custom
  harness use `source: account` (the existing bridge's configured model). Native
  Claude/Codex harnesses and native executors require `source: explicit`.
  OpenCode/custom permits account or explicit model, preserving its existing pin
  precedence and routing options; its account must still configure the executor
  role and a model as required by the custom-account contract. `dsh` (opt-in,
  off unless `ARGUS_DSH_ENABLED=true` on the API; saving or admitting a `dsh`
  route while disabled fails with `dsh_disabled`) follows the same `local`
  (`source: account`) and `custom` (`source: account` or `explicit`) contract
  without a DeepSeek-native provider. When a `custom` account referenced by a
  `dsh` executor has no `executor` model, the route uses the account's `engine`
  model; if neither is configured, validation and admission fail explicitly with
  `executor_model_missing`.
- `mode: single` requires `harness` and **forbids** `executor`, including null.
  `mode: paired` requires both selectors. Unknown fields, duplicate IDs within a
  difficulty, unknown difficulty/version, invalid provider/adapter and unsupported
  model source are `422 validation_failed` with a field path and stable reason.
- A selector may set `effort`, the reasoning effort for that role only (3.8).
  Omitted means the runtime's own default, exactly as before this field existed.
- Each named difficulty has 1–16 alternatives; IDs use the existing bounded
  identifier convention (1–64 ASCII letters, digits, `_` or `-`). Empty list is
  invalid, not an ambiguous spelling of single-layer or inherit.

## 3.2 Runtime capability is not a provider or model

`runtime` identifies the worker execution adapter; `harness.adapter` identifies
its client/agent loop; `provider` identifies the account lane. Thus
`nanoclaw` + `claude` + `local` means the existing local harness transport, not
Anthropic service, not a model named Claude, and not a standalone OpenCode loop.

The initial implementation targets these existing adapter combinations:

| Runtime | Harness adapter | Account provider | Single | Paired |
| --- | --- | --- | --- | --- |
| NanoClaw | `claude` | `anthropic` | target | target |
| NanoClaw | `claude` | `local` | target | target |
| NanoClaw | `claude` | `custom` | target | target |
| NanoClaw | `codex` | `openai` | target | target |
| Any | `agent`, `opencode`, `copilot`, `agy`, `dsh` | any | unsupported | not harnesses |

The matrix mirrors what the NanoClaw worker runs: harness engines are `claude`
(anthropic, local, custom) and `codex` (openai); each provider has exactly one
delegate CLI (`EXECUTOR_BY_PROVIDER` in `integrations/nanoclaw/argus-accounts.ts`).
Intentionally unsupported, because the worker cannot run them: `codex` on
local/custom (the Codex harness speaks only OpenAI), `opencode`/`agent`/`copilot`/`agy` as
harnesses (executor-only CLIs), and an executor adapter on a provider other than
its own (for example `claude` on `custom`). A local harness with a `claude`, `codex`,
`copilot` or `agy` executor stays refused: those CLIs would run on the redirected local
endpoint. Setup shows each adapter/provider as harness-capable or executor-only and
disables an executor option with its reason.
| Built-in orchestrator | any | any | unsupported as task worker | unsupported |

“Target” is a required adapter conformance test, **not a claim of shipped chain
support or model tool-use quality**. The current empty-executor path is generic
in `argus.ts`; the separate A/B experiment explicitly supports only Claude on
Anthropic. Its eligibility must not be reused to silently exclude local direct
routes or to claim all providers are verified. Do not enable its env flags.

Paired executors are `claude`/`anthropic`, `codex`/`openai`, `agent`/`cursor`,
`copilot`/`copilot`, `agy`/`google`, `opencode`/`local`, `opencode`/`custom`,
`dsh`/`local`, and `dsh`/`custom` (the `dsh` entries are opt-in and listed by
`GET /api/engines` only when `ARGUS_DSH_ENABLED=true`), subject to the
existing account role and harness pairing restrictions. In particular a local
harness must not run `argus-delegate claude` on its redirected endpoint; the
local pair accepts `agent`, `opencode` or `dsh`, matching pinned-route
validation. Do not weaken redirect guards to expand the matrix. Custom harnesses
have their own bridge-environment stripping in `argus-delegate.mjs`; do not copy
local-only pair restrictions to them. Native same-provider pairs must name the
same account: the existing gateway has one native identity per provider in a
session, not separate harness/executor identities. Distinct custom accounts are
allowed through their existing separately scoped role lanes. These static rules
are implemented in the core contract, not proof of runtime conformance.

One API capability catalogue, exposed as `execution_capabilities` on
`GET /api/engines`, describes supported runtime/mode/adapter/provider/model-source
combinations and pair restrictions. Settings validation, diagnostics and Setup
use it, not the dashboard's shared `KNOWN_ENGINES` list. Static unsupported
combinations are rejected on write even if a later fallback is valid. Missing
worker capability is an admission reason, not permission to reinterpret a
route. Workers register an execution-contract version and supported capability
IDs; old workers can run legacy tasks only. No eligible installed worker may be
reported as ready merely because a provider has an account.

The API validates references, role, provider and model-source compatibility on
write. Disabled or exhausted accounts may be saved, with readiness warnings.
A later account deletion or role/model change makes that alternative unavailable
with a diagnostic reason; the route never silently points to another account.
The catalogue proves adapter support only, not endpoint reachability, gateway
authorization, installed CLI readiness or successful model tool calling.

## 3.3 Inheritance, compatibility and explicit migration

- Default is `{version: 1, difficulties: {}}`: no explicit difficulty chain.
- Resolution is **per difficulty**, project entry over global entry. Missing
  project entry inherits global; missing both uses the unchanged legacy path.
  A project list replaces a whole list, never merges alternatives or selectors.
  Reset one difficulty removes its entry; resetting the setting removes the
  scoped row. An empty map at project scope inherits every global difficulty.
- Effective responses include each difficulty's source (`project`, `global`,
  `legacy`) and revision. A stored scope map is returned separately so an editor
  does not materialize inherited values on Save. Each revision hashes canonical
  JSON of contract version, difficulty, source and its complete effective list.
  Account health and unrelated difficulty edits do not change that revision.
- Without an explicit chain the existing `difficulty.executors`, model maps,
  provider ranking, soft local preference, env locks and task-pin fallback are
  byte-for-byte compatible. `difficulty.executors: {trivial: []}` alone still
  does **not** promise a local harness.
- An explicit chain owns both layers and models for that difficulty. Legacy
  `models` and `difficulty.executors` cannot supply hidden alternatives. If
  `ARGUS_MODEL_POLICY` or `ARGUS_DIFFICULTY_EXECUTORS` is nonempty, saving an
  explicit chain is refused with `legacy_env_lock`; if a lock later appears,
  admission blocks the explicit route with that reason, rather than ignoring
  the lock or silently reverting to legacy. Unconfigured levels remain legacy.
- Existing `harness_model`, `harness_account` or `executor_model` task pins take
  precedence by choosing the **legacy task-pin path as a whole**. Diagnostics and
  Setup preview explicitly say `legacy_task_pin`, the bypassed chain and the
  existing pin/fallback semantics. Never overlay a pin onto half a configured
  pair. `account_providers` without a pin intersects each alternative's harness
  provider, never its executor. No compatible alternative means wait, not cloud
  fallback. This preserves existing pins without inventing mixed combinations.
- No automatic DB rewrite or migration that freezes whichever account wins
  today. Setup may produce an editable migration preview, but requires the
  operator to choose every account/model and save explicitly. A legacy CLI
  sequence cannot be losslessly converted to complete alternatives.
- Keep legacy fields/endpoints/tools for old callers. A task using an explicit
  chain gets the new execution contract, not a misleading legacy projection
  that an older channel could execute differently. Removing the scoped entry
  restores inheritance/legacy for future claims; running attempts keep snapshots.

## 3.4 Admission, atomic leases and the running contract

A shared API service resolves the route for claim, lease, availability, waking,
quota/failure handling and read-only diagnostics. No second resolver lives in
MCP, the browser or the channel. Resolve restrictions and capability support
before reserving anything. Evaluate alternatives in configured order starting
at the persisted cursor; do not rank accounts outside their exact selectors.

For a paired alternative, both accounts must pass existing role, credential,
health, capacity and caller-runtime checks **in the same Database.write()
transaction**. Reserve the complete tuple or neither. A rejected second lease
must leave no first lease, claim, gateway grant or spent attempt. Reserve a
single-layer route's engine only: never probe, wait for, lease, configure or
invoke an executor. No request with an omitted task/attempt can reserve a chain.

Unpinned selectors are resolved at selection time: the harness takes the best
usable account of its provider that can serve the role and name a model (a local
account whose discovered models exclude its model is skipped for the next one); an
executor does the same. A same-provider native pair runs on one account: an
unpinned side adopts the other side's pin, or both adopt the harness's resolved
account. Save rejects only two different pins. Availability and wake evidence try
the same assignments. The concrete account and lease are in
`ExecutionSelection.harness/executor.account_id`, which the
`task.execution_route_selected` event and the attempt snapshot keep.

Native same-account reuse follows existing safe role semantics; distinct
providers/accounts use distinct leases. A custom harness bridge does not grant
executor access: OpenCode/custom always requires its separately issued scoped
executor lease, even for the same account, with capacity for both roles. Preserve
PR #407 and its `account_provider: custom` behavior on the legacy pin path.
Do not increase `max_leases` to make a pair fit.

An idle held engine lease can be retained only if its exact account and role fit
the selected alternative. Otherwise stop the old runtime through the existing
switch handshake before applying the new tuple. A failed stop blocks with the
existing runtime-switch failure; never run new model settings in the old session.

Claim delivers `execution` with contract version, source, policy revision,
difficulty, selected alternative ID/index, mode, runtime, both configured
selectors and resolved account/model facts, role-to-lease IDs, and a selection
generation. Single-layer omits executor data. The attempt stores the same
non-secret snapshot and the candidate list/revision used to select it. Model
policy files remain for legacy use; explicit route model selection cannot walk
an independent policy fallback chain or use failure count as a model index.
Account credentials/endpoints stay exclusively in existing lease delivery.

The generation and task/attempt/agent fence apply to lease, model-selection and
transition reports. Stale or duplicate reports cannot release a newer tuple or
advance twice. Before starting execution the channel acknowledges that exact
contract/generation (`action: "applied"`), which still refuses with `409`
`execution_lease_missing` if the tuple is no longer owned. A channel without
support must refuse it, not run legacy. When credential application fails on a
claimed execution-chain tuple, the channel never issues a blanket
`/accounts/release`; it preserves the reserved tuple for the fenced
`action: "advance"` (naming `failed_role` and classified `reason`, e.g.
`auth_expired` for expired/rejected credentials), and falls back to a sanitized
`/fail` plus local claim cleanup if the report itself fails so the claim can
never strand. If `action: "advance"` arrives for a live claim and matching
generation after tuple ownership was already lost externally, the API releases
only any still-active lease of that exact tuple, increments generation, and
fails the attempt with transient reason `execution_lease_missing` rather than
leaving the task claimed.

## 3.5 Fallback and exhaustion

Fallback means **advance to another complete alternative**, with evidence, not
swap one layer or keep a dangling executor from the previous alternative.
Before advancing the alternative cursor or declaring chain exhaustion on an
account-scoped `quota` or `auth_expired` failure, an **unpinned** selector
performs bounded **same-alternative account failover**:

- Record the failed account ID per alternative and role (`failed_accounts` in
  `task_execution_routes`) for the current unresolved quota window, so each
  account is tried at most once per alternative and never bounced back to if its
  cooldown elapses during the same window. Preserve a provider-reported `until`
  reset time when supplied on `ExecutionReport`; otherwise apply the existing
  cooldown without fabricating a stated reset.
- Keep `cursor` on the current alternative (preserving the exact alternative,
  role, adapter, and model — including both models of a paired route when an
  executor fails) if another compatible, non-disabled, non-auth-rejected,
  non-exhausted, unexcluded account exists for that selector.
- If a compatible replacement account is currently at `max_leases` capacity,
  treat it as busy-slot waiting (`awaiting_execution_route` /
  `task.execution_route_busy_wait`), not quota exhaustion.
- If the failed role is pinned (`account_id` set, or either side of a
  same-provider native pair pinned) or no eligible replacement account remains,
  advance `cursor` to `index + 1` (or block `execution_chain_exhausted` after the
  final alternative).

| Condition | Action | Task accounting |
| --- | --- | --- |
| Missing credential, disabled/full account, unserved required local model, unavailable runtime/CLI before start | Record bounded skip reason; inspect next alternative | No attempt or failure spent |
| No complete alternative currently admissible | Block `awaiting_execution_route`; wake on capacity/health/runtime changes | No new attempt; cursor does not permanently blacklist transient skips |
| Quota/provider unavailability after start | Stop runtime; release tuple; advance cursor; requeue for next complete alternative | Existing quota handling: no task failure; no simultaneous sessions |
| Provider/model/CLI protocol failure classified as route failure | Stop and release; advance; next ordinary claim is a new attempt | Count one failure under existing retry limits |
| Context overflow, verification failure or review rejection | Apply existing difficulty escalation; resolve the new difficulty next claim | Existing failure/review accounting; record route invalidation |
| Disk/budget/security denial, stale claim or failure to stop old runtime | Block/reject under existing guard | Never fall through to evade the guard |
| Ordinary task/implementation failure without route failure classification | Existing task retry on same alternative | Existing retry budget; no unrequested model switch |

Route failures use an allowlist of existing classified codes (quota/provider
unavailable, `auth_missing`, `auth_expired`, `not_installed`, `model_not_served`, provider
protocol/transport failure); raw model text never selects a fallback. A gateway
access denial is a security denial, not an auth-missing route failure. Unknown
failure stays ordinary task failure. Credential rejection may disable/exhaust
only through existing account rules; the chain creates no new auth retry loop.
`auth_expired` (an expired login) blocks the failed account as in [failure
classes](failure-classes.spec.md) 3.2, advances the cursor, and spends no failure.

Persist cursor and transition generation with task routing state, keyed by a
fingerprint of route-affecting task fields (not the event/state version),
difficulty and effective policy revision. Progress, lease renewal and ordinary
claim state transitions must not reset the traversal. The current
attempt always uses its frozen snapshot. A quota transition releases the tuple
and requeues atomically after stop acknowledgement; a crash is recovered through
existing lease expiry/reaper fencing, not a second live run. On restart do not
return to the first already failed alternative by losing in-memory state.

Preflight skips are transient and reconsidered on wake. A runtime failure
advances monotonically within a traversal; after the final alternative, block
with `execution_chain_exhausted` and diagnostic evidence. No wraparound, legacy
cloud fallback or automatic paid retry. Managers can explicitly retry/reset the
route traversal using the existing retry/unblock surfaces extended with
`reset_execution_route: true`, with an audit event and normal state guards.
A policy/difficulty/task-definition change starts a new traversal on the next
claim, with an invalidation event. No change affects the active attempt.

## 3.6 Evidence and operator surfaces

Reuse `get_settings`, `set_setting`, `reset_setting`, task claim and
`get_task_route` across REST and MCP. Add typed setting/claim/diagnostic models
and regenerate OpenAPI; transport wrappers call the shared resolver. Mutating
selection/stop acknowledgements are worker-and-live-claim scoped, not admin
settings writes. Extend retry/unblock and their MCP wrappers together.

Record `task.execution_route_selected`, `task.execution_route_skipped`,
`task.execution_route_account_failover`, `task.execution_route_busy_wait`,
`task.execution_route_advanced`, `task.execution_route_exhausted` and
`task.execution_route_reset` with revision, difficulty, alternative ID/index,
mode, configured/resolved model, account IDs (old/new on failover), role,
generation, failure classification, reason code and from/to IDs where applicable.
Deduplicate repeated preflight skips by route
revision, availability reason and alternative; bound detail, redact errors,
never include credentials, arbitrary response bodies or URL query strings.
Model execution records retain configured versus observed IDs separately;
unknown observation is unknown, not proof that the configured model ran.

`#setup` currently defaults to Settings (`#setup/settings`); both must expose
**Execution chains**, with a global/project picker and all four difficulties.
Each difficulty has ordered cards: Single layer / Harness + executor, supported
runtime, adapter, provider, account (default "Any available account", optional pin),
allowed model-source selectors and, per role, an effort select offering "Runtime
default" plus the catalogue levels the chosen model takes (3.8). An unsupported
adapter disables it with its reason; an inherited list shows each role's effort.
Add/remove/reorder changes the **whole alternative**. Show source, effective
inherited list, editable scoped list, save validation and per-level inherit/reset.
Save validation lists every problem above the Save button, one line each, naming
the difficulty and alternative (`hard, alternative 2: executor model ID is empty`),
and Save is `aria-describedby` that list. A missing capability list, a legacy
environment lock or a save in flight is listed the same way and also disables Save.
A difficulty without a chain shows its built-in (or stored `difficulty.executors`)
executor order, read only, labelled as legacy routing; Setup has no separate
Difficulty executors card. Never present the `trivial` model-policy map as a
local harness selector.

Show local/custom configured account model next to discovered/observed model
facts and their timestamps, plus existing stale-model warnings. No hardcoded
model ID, inferred model replacement, implicit inference test or account update.
Unsupported choices are disabled with a reason; API rejects the same combination.
The ordered preview labels every paid/cloud alternative, including its executor,
without claiming a spend cap; no paid alternative is preselected or appended.
A direct route shows “No executor”; a paired route shows both accounts/models.
Task detail/route diagnostic shows selected ID, cursor, all skip/advance reasons,
last recorded selection versus current eligibility, pins/locks and resets.

## 3.7 Independent reviewer routes

The independent reviewer has no chain of its own: it walks `execution.chains`, from the
reviewed task's difficulty and then the other difficulties, to the first alternative
whose model differs from the author's ([settings](settings.spec.md) 3.5). When an independent `code_review` child is
created, Argus drops the author alternatives and stores the remaining ordered list
in `tasks.execution_route` (migration `0086_task_execution_route.sql`) with source
`review` and the parent ID, difficulty, chosen ID and skipped alternatives. That
explicit route replaces the generic chain for the child's own difficulty; admission,
fallback, exhaustion, reset and diagnostics then work as above on that list. A new
`execution.chains` edit does not re-route an existing child. A void verdict creates a
new child with the void model's accounts treated as authors. A child that ends
`failed` or `cancelled` without a verdict does the same for the failed
alternative and any alternative pinned to its account, which are skipped as
`failed` ([settings](settings.spec.md) 3.5, bounded by `max_reviewer_failures`);
when none remains the parent blocks as `review_failed` and a human decides.

## 3.8 Reasoning effort per role

`effort` is an optional selector field, set independently on the harness and, in
a paired alternative, on the executor. It is a lowercase level name; there is no
universal vocabulary, so each adapter/provider has its own verified set and
interface. The capability catalogue carries the set (`efforts`), the interface
(`effort_interface`), per-model rules (`effort_model_rules`) and how unlisted
models are treated (`effort_unlisted_models`). Settings writes validate against
it; Setup renders from it.

| Role | Adapter / provider | Levels | Runtime interface | Model rules |
| --- | --- | --- | --- | --- |
| harness | `claude` / `anthropic` | low, medium, high, xhigh, max | NanoClaw container config `effort` → Agent SDK `query({effort})` | Claude CLI rules; unlisted models supported |
| harness | `codex` / `openai` | low, medium, high, xhigh | NanoClaw container config `effort` → Codex app-server `model_reasoning_effort` | Codex catalogue; unlisted models unverified |
| harness | `claude` / `local`, `claude` / `custom` | unsupported | the redirect and bridge do not translate effort | — |
| executor | `claude` / `anthropic` | low, medium, high, xhigh, max | `claude --effort <level>` | Claude CLI rules; unlisted models supported |
| executor | `codex` / `openai` | low, medium, high, xhigh, max, ultra | `codex exec -c model_reasoning_effort="<level>"` | Codex catalogue; unlisted models unverified |
| executor | `copilot` / `copilot` | none, minimal, low, medium, high, xhigh, max | `copilot --reasoning-effort <level>` | none known; all models unverified |
| executor | `agy` / `google` | low, medium, high, xhigh, max | `agy --effort <level>` | agy 1.5.12 model list; unlisted models unverified |
| executor | `agent` / `cursor` | unsupported | only bracketed model parameters, which model IDs forbid | — |
| executor | `opencode` / `local`, `opencode` / `custom` | unsupported | `--variant` needs provider variants Argus does not define | — |
| executor | `dsh` / `local`, `dsh` / `custom` | unsupported | no configured effort interface in the headless overlay | — |

Sources: claude-code 2.1.296 (`--effort`, per-model support functions), Claude
Agent SDK 0.3.296 (`EffortLevel`), codex-cli 0.162.1 (bundled model catalogue
`supported_reasoning_levels`), the NanoClaw agent-runner's `normalizeCodexEffort`
(none, minimal, low, medium, high, xhigh), copilot 1.0.105 (`--reasoning-effort`),
agy 1.5.12 (`--effort` and `agy models`), cursor-agent 2026.10.16 and the pinned NanoClaw host, whose `container_configs`
row has an `effort` column written to `container.json`. All from help text,
type definitions and static inspection; no inference was run.

Model rules match the canonical model ID (lowercase, trailing `-YYYYMMDD`
removed). Claude: `claude-3-*`, `claude-opus-4`, `claude-opus-4-0`,
`claude-opus-4-1`, `claude-sonnet-4`, `claude-sonnet-4-0`, `claude-sonnet-4-5`
and `claude-haiku-4-5` take no effort; `claude-opus-4-5` takes low–high;
`claude-opus-4-6` and `claude-sonnet-4-6` take low–high and max. Codex:
`gpt-5.5` takes low–xhigh; `gpt-6-luna`, `gpt-5.6-luna` and `codex-auto-review`
add max; `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6-sol`, `gpt-5.6-sol`,
`gpt-5.6-terra`, `gpt-daybreak-blue-latest` and `gpt-daybreak-red-latest` add
max and ultra; a harness level is always also within the harness set.

- An effort on an unsupported adapter/provider is `422` with
  `<role>_effort_unsupported`; a level outside the set is `<role>_effort_invalid`;
  a level a listed model does not take is `<role>_effort_unsupported_by_model`.
  Every supported row uses an explicit model, so its rules are known on save.
  Nothing is silently
  dropped or clamped; the Claude CLI and SDK would downgrade silently, so Argus
  refuses those combinations before saving.
- An unlisted model on an "unverified" adapter is accepted and Setup labels the
  level "unverified for this model"; the CLI decides at run time.
- The field is excluded when omitted, so existing settings, revisions, snapshots
  and fingerprints are byte-for-byte unchanged. Setting, changing or clearing it
  changes only that difficulty's revision, which starts a new traversal there on
  the next claim (3.5); other difficulties and generation fencing are untouched.
  It travels in the alternative: claim snapshot, candidates, `review` routes,
  `task.execution_route_selected` and every fallback alternative keep the level
  configured for that alternative, never the previous one's.
- NanoClaw applies the harness level to the group container config with the
  model, restarting the session when either changes, and restores the operator's
  previous value when a later route omits effort. `argus-delegate` adds the
  executor flag above and refuses an effort for an adapter without an interface.
  Both re-check the set and refuse an unknown level rather than ignoring it.
- This is the configured level. No adapter reports the level it applied, so
  observed effort is recorded as unknown (`null`), never as the configured value.
- Author exclusion in independent review compares provider, model and account
  pins, not effort: the same model at another effort is still the author.

# 4. Open Questions

No unresolved question blocks the contract above. Later extensions (standalone OpenCode/Cursor harness adapters, alternative-level
retry policies, or controlled model overrides for local harnesses) need separate
capabilities and conformance tests. They are not implicit behavior in version 1.

# 5. Constraints

- Single SQLite node; additive next-numbered migrations only. Routing state and
  audit writes use `Database.write()` and `tx.now`; never inference/network I/O
  inside a transaction. Discover/probe outside, consume recorded evidence inside.
- Keep task scope, token roles, lease roles, redirect/SSRF guards, gateway secret
  assignment, workspace trust, command guards, project runtime and budget gates.
- Versioned opt-in rollout: old workers keep legacy behavior, explicit chains
  cannot be claimed by them. Do not mark capability support from account presence.
- All implementation tests use fixtures, fake providers and disposable databases.
  No paid calls, production account edits or deploys are needed for this feature.

# 6. Implementation Consequences

This exceeds a safe local-only patch: current engine admission lives in
`tasks/engine.py` and `accounts/service.py`, executor acquisition happens later
in the NanoClaw channel, and `model-policy.mjs` selects models independently.
These boundaries must change coherently; shipping one new dropdown first would
promise strictness the runtime cannot deliver.

The implementation follows these dependency-ordered local acceptance slices.
Publication and live activation are separate from their local verification.

1. **Core contract and capability matrix.** Create
   `packages/core/src/argus_core/execution_chains.py`; extend `engines.py`,
   `settings.py` and `tasks.py` there. Add
   `packages/core/tests/test_execution_chains.py`: closed unions, limits, all
   four difficulties, account-model sources, invalid runtime/mode/provider
   combinations, mixed alternatives and no executor field for single mode.
   First prove rejection/roundtrip tests fail; then add typed models and matrix.
2. **Settings and read-only resolver.** Extend
   `apps/api/src/argus_api/settings/{registry,service}.py`; create
   `apps/api/src/argus_api/tasks/execution_routes.py`. Extend `engines/routes.py`,
   `tasks/route.py`, `tasks/routes.py` and `mcp_server.py` through shared models.
   Extend `apps/api/tests/test_settings.py` and add
   `apps/api/tests/test_execution_routes.py`: persistence/reopen, per-level
   project inheritance/reset, env locks, task-pin bypass, provider restrictions,
   account deletion/role changes, diagnostics redaction and no network writes.
   Expose capability/preview data before allowing execution admission.
3. **Atomic reservation.** Extend `accounts/service.py`, `tasks/engine.py`,
   `tasks/claim.py`, and attempt snapshots. Add a savepoint-safe tuple selection
   path; retain legacy code when no explicit chain applies. Test competing claims,
   full second account rollback, held-engine mismatch, native reuse, custom
   separate leases/capacity and single-mode engine-only reservation. Assert
   account priority cannot combine one alternative's harness with another's
   executor. Include local harness + OpenCode/custom and nonlocal direct cases.
4. **Durable traversal and worker protocol.** Add the next migration under
   `apps/api/src/argus_api/db/migrations/` for route snapshot/cursor/generation
   state; extend worker registration capabilities, claim contracts and scoped
   stop/selection acknowledgements in `agents`/`tasks` routes and core models.
   Test restart/reopen, stale/duplicate reports, no wraparound, transient skip
   wake, post-start quota, ordinary failure, guard denial and difficulty
   escalation. Extend reaper/waking and manager retry/unblock reset together.
   All state transitions use the slice-2 resolver and slice-3 tuple path.
5. **NanoClaw execution and model selection.** Extend
   `integrations/nanoclaw/{argus.ts,argus-accounts.ts,model-policy.mjs,argus-delegate.mjs}`.
   Use the exact execution snapshot, never `executorFirst` or account-ranked
   model selection on this path. Extend `argus-accounts.test.ts`, `argus.test.ts`,
   `argus-models.test.mjs` and the delegate tests: mixed paired→single and
   single→paired transitions, local direct without executor calls/probes, native
   direct adapters, custom bridge model, OpenCode/custom separate scoped lease,
   restart/stop handoff, fixed model after ordinary retry, and old-worker refusal.
   Keep experiment code and flags untouched; add conformance fixtures for every
   advertised capability before exposing it as supported.
6. **Dashboard editing and evidence.** Extend
   `apps/dashboard/src/{settings.ts,models.ts,views/Settings.tsx,views/ModelPreferences.tsx}`;
   create `views/ExecutionChains.tsx` and its existing-style component tests.
   Reuse account/local model facts and capability catalogue, not handwritten
   provider lists. Test all difficulty cards, order and mode changes, exact
   payloads, project partial saves, reset, unsupported validation, stale models,
   legacy pins/locks, mixed fallback preview and no account mutation. Exercise
   `#setup` and `#setup/settings` with an isolated fixture API/browser and save
   screenshots; do not require preview credentials.
7. **Compatibility and acceptance gate.** Regenerate clients with `make openapi`;
   extend legacy routing/pins/quota/security tests instead of rewriting shipped
   migrations. Run core/API tests plus provisioned NanoClaw integration tests,
   `make format`, `make lint`, `make typecheck`, `make check`, and `make build`.
   If MCP descriptions/schema change, record `make tool-tokens` before/after.
   Update affected living specs to implemented only after evidence. Keep live
   rollout/real inference a separately authorized operator task.

## Verification boundary

- Core contract, settings validation/inheritance, capability catalogue, route
  diagnostics and REST/MCP use the shared `tasks/execution_routes.py` service.
  Migration `0085_execution_routes.sql` stores cursor, fingerprint, generation,
  last selection and bounded skip evidence. Attempt definitions freeze the
  candidate list and exact selected tuple without credentials.
- Tuple reservation uses existing account eligibility inside savepoints within
  the claim transaction. Native reuse retains one lease; custom roles require
  separate capacity. Unsupported workers cannot claim explicit routes.
- `POST /tasks/{id}/execution` (and `report_execution_route`) acknowledges an exact
  attempt/generation or advances only with `stopped: true` and an allowlisted
  reason. Exact lease IDs, account IDs, roles and agent ownership are checked;
  stale/duplicate transitions cannot release or advance a newer selection.
  Reset increments generation rather than deleting its identity. Failed stops
  block and retain explicit tuple capacity until confirmed stop or reaping.
- NanoClaw consumes the reserved leases without account ranking, fixes harness
  and delegate models from the snapshot, refuses unsupported contracts and stops
  before advancing. Single mode performs no executor lease/probe. The independent
  single-layer experiment remains disabled/unmodified in behavior.
- Workers declare `execution-chains-v1` plus each installed adapter capability ID
  from `GET /api/engines` in their existing capabilities registration/claim. These
  declarations are operator/runtime evidence, not inferred from account presence
  or model/recycle callbacks. An old installation with no such declaration stays
  legacy-only. Do not declare an adapter whose CLI is not installed.
- Setup Settings exposes all four difficulties at global/project scope. Ordered
  cards edit whole alternatives; scoped Save never materializes inherited lists.
  Capability-driven provider/source controls and server validation share the
  matrix. Task detail exposes the last selection, cursor and diagnostic evidence.
- Local tests use disposable databases, memory secrets, fake workers, fake CLIs
  and a fixture browser API. They cover all four difficulties, both mixed-mode
  orders across database reopen, exact tuple rollback/reuse/capacity, generation
  fencing, reset, model freezing, pins/locks and no-executor calls. Repository,
  provisioned NanoClaw and delegate suites must be green for publication.
- Effort (3.8): core, settings, snapshot and fallback tests, delegate argv tests
  and NanoClaw container-config tests check the configured level and the exact
  constructed invocation. Whether a provider honours a level is not verified.
- Real-provider inference, installed production CLI readiness, gateway grants,
  deployment and live activation remain separately authorized operator work.
  No local test proves endpoint reachability or model tool-use quality. No live
  account, setting, model, service, image or capacity is changed here.
