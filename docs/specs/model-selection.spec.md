# Preferred models

# 1. Purpose and Scope

Separate NanoClaw harness models from delegated implementation models. A JSON
policy chooses ordered model chains per provider/executor, with immutable
content-derived revisions and attempt evidence. No deployment is performed.

# 2. Non-Goals

Changing accounts, authenticating, switching providers on model failure, or
claiming a provider actually served a model without provider evidence. No new
service or dependency and no dashboard-specific policy editor.

# 3. Decisions Made

`ARGUS_MODEL_POLICY` is a complete JSON policy, editable in existing pool env
configuration. Missing means the checked-in default; malformed means fail closed.
The policy has `harness` and `implementation` maps, each mapping an engine to
an ordered nonempty list. Model IDs are opaque CLI IDs, not availability claims.
Defaults: Codex harness `gpt-6-luna`, Codex implementation `gpt-6-luna` (the account's own configured model — the default is a placeholder, edit it), Claude
`claude-sonnet-5-5` then `claude-opus-5-5`. Operators must use IDs their provider
supports; no alias rewriting. Other executors keep their native default unless
configured. Revisions are SHA-256 of canonical policy content.

At each claim, the channel applies the harness selection before injecting work,
using the existing NanoClaw model setting, killing stale session containers when
it changes. Task failures advance the harness chain (bounded at the last model);
review rejection also advances it. Questions/account exhaustion alone do not.
The harness chain used is the one of the engine the leased account selected
(global account priority, [account selection](account-selection.spec.md)); a
policy without a chain for it fails the task rather than keeping another
engine's model. The copy pinned to the workspace carries `engine`,
`account_provider` and `executor_account_provider` in its context (never a
credential) and `argus-delegate` reports them.
An attempt artifact stores policy revision, requested model, configured model,
`resolved_model: null` (provider resolution is not observable), and fallback
reason. The entire policy and task identity are copied to the agent workspace
so delegated runs use the same revision, not an unrelated process default.

`argus-delegate` selects implementation defaults and accepts `--model` (pins,
no automatic fallback), `--model-attempt N` and `--fallback-reason REASON` for
explicit retries after a failed gate/context limit. A failed CLI run advances
its configured chain; missing authentication/binary, quota, timeout and signals
never trigger automatic model escalation. Each invocation is bounded by one
shared timeout, with separate logs and durable JSON evidence for every model
attempt. No executor/provider switch. CLI success is not verification success;
the caller must run gates and explicitly request the next model on rejection.

# 4. Open Questions

Provider-observed model IDs are not uniformly exposed by CLIs. They remain
unknown rather than being inferred from the model sent in argv. The evidence
rule and the per-task record of observed models live in
[model observability](model-observability.spec.md). A dedicated UI
can follow; the existing pool env editor/API is the configuration surface now.

# 5. Constraints

Policy updates apply on new instances (pool env follows existing lifecycle).
In-flight attempts retain their snapshot. No real credentials in the policy.
Fallback runs may see partial workspace edits, so callers must review the diff.

# 6. Implementation Consequences

Install the shared policy module into both host and agent image, rebuild both
through the existing provisioning flow, and recreate instances to change pool
env. Attempt artifacts remain available through existing task artifact APIs.
See [operator instructions](../runbooks/2026-10-17.model-selection.ref.md).
