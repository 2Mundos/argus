---
status: implemented
created: 2026-11-04
updated: 2026-11-05
---

# Single-layer experiment: the Claude harness codes without the delegate hop

# 1. Purpose and Scope

P7 of [worker efficiency](../drafts/2026-10-24.worker-efficiency.draft.md) proposes
that for coding tasks the coding model is the engine: Claude Code with the
implementation-tier model works in the task workspace, with no cheaper harness model
and no `/usr/local/bin/argus-delegate` hop. The draft asks for an A/B on about 20
comparable tasks before [engines](engines.spec.md) changes. This spec defines that
experiment: the opt-in arm in the NanoClaw channel, how tasks are assigned, what is
recorded, and how the arms are compared.

**No experiment result exists.** The arm and the analysis are instrumented and
tested against fakes and synthetic fixtures only. Figures from those tests are not
results, and nothing in this spec says the single-layer arm is better or worse.

The scope:
- `integrations/nanoclaw/argus-single-layer.ts`, wired into `argus.ts`, and its
  vitest `argus-single-layer.test.ts`.
- `scripts/single_layer_ab.py` (assign, export, analyze), its test
  `scripts/test_single_layer_ab.py`, and the fixtures under
  `scripts/fixtures/single-layer-ab/`.
- The operator procedure, in the
  [single-layer experiment runbook](../runbooks/2026-11-04.single-layer-experiment.ref.md).

# 2. Non-Goals

- No change to the default path's behaviour. With the flag off, routing, lease and
  account handling, prompt and model selection are today's. The progress stream
  is not byte-for-byte: the channel's `pr_opened` relay (`relayPullRequests`,
  `argus.ts:4353`, called from `argus.ts:4525`) runs in every execution mode,
  including the default path.
  - The delegated arm and the default path are identical in routing, lease and
    account handling, prompt rendering and model selection. The arm is decided
    after the engine lease is held (`argus.ts:3406`) and is read only to prepare
    models (`argus.ts:3014`), to record it (`argus.ts:3436`) and to render the
    prompt (`argus.ts:3491`). The delegated arm adds only records (3.5).
  - The single-layer arm shares the default path's routing and lease code. Its
    prompt and model selection differ by design (3.3).
  - The `pr_opened` relay is a report-stream difference, not a behaviour
    difference in the arm under test, so it does not invalidate the experiment.
- No engine other than the Claude harness on an `anthropic` lease (3.2).
- No move of push, PR or CI work from the prompt into adapter code. That is the
  second half of P7 and waits for a result.
- No API, schema, dashboard or MCP change. The arm is read from data that is already
  recorded (3.5).
- No paid-run scheduling, no automatic merge or deploy, and no automatic decision.
  An operator starts the runs, and the decision is a human one, made with the
  analysis output (6).

# 3. Decisions Made

## 3.1 The flag

The arm is off unless the NanoClaw host process starts with
`ARGUS_SINGLE_LAYER=claude` and `ARGUS_SINGLE_LAYER_ASSIGNMENT=<file>`. An empty
value, `0` or `off` is off. Any other value is refused: `unsupported_engine` for
`codex`, `cursor`, `local`, `custom`, `copilot` or anything else, and
`no_assignment` when the file is unset. A refusal logs a warning and runs the
default path. The flag is read at host start, so turning it on or off needs a
restart.

## 3.2 Assignment and eligibility

The assignment file is `{experiment, engine: "claude", arms: {task_id: arm}}`, with
`arm` `single_layer` or `delegated`. `scripts/single_layer_ab.py assign` writes it
(3.6). The channel reads it at every claim, after the engine lease. A task not in
the file is not in the experiment, and its claim is unchanged. A file that is
missing or malformed (no valid experiment id, wrong engine, an unknown arm) logs a
warning, and the claim runs the default path unmarked.

A claim assigned `single_layer` runs single-layer only when every precondition
holds. The first one that fails is the fallback reason:

| Reason | Precondition |
| --- | --- |
| `no_engine_lease` | the claim holds an engine account |
| `engine_not_claude` | the lease is the Claude harness on `anthropic` |
| `harness_model_pinned` | the task pins no harness model |
| `executor_model_pinned` | the task pins no executor model |
| `no_delegated_executor` | the difficulty has a delegated executor (if not, both arms are already the same direct run) |
| `no_model_policy` | the host has a model policy to select from |
| `no_implementation_chain` | the policy has a Claude implementation-tier chain |

A fallback runs the default path: the same harness model, prompt and delegate as an
unassigned claim. It is never emulated through the delegate. The `delegated` arm is
always the default path, marked.

## 3.3 What changes in the single-layer arm

- **Harness model.** The harness takes the Claude chain of the implementation scope
  that the delegate would have used for a Claude executor (`implementationScope`:
  `implementation`, `hard_implementation` or `trivial`), at the position the harness
  chain would take on a retry. See [model selection](model-selection.spec.md).
- **Prompt.** The delegation block is replaced by "you are the coding model; do not
  call `argus-delegate` or start another coding CLI; name the PR URL in a progress
  message or in `COMPLETE`". Everything else in the task body is unchanged.
- **`pr_opened`.** The channel reports the milestone itself, as in delegated runs.
  A URL in a reply whose base is the task's repository (compared case-insensitively)
  and whose path ends `/pull/<n>` or `/pulls/<n>` is posted as `pr_opened`
  `{number, url}`, once per URL and attempt, before the reply is handled
  ([progress milestones](progress-milestones.spec.md)). A 409 claim-lost answer
  abandons the attempt, as on any fenced call.

Unchanged in both arms: routing and account leases, isolation and the workspace,
task protocol directives, structured PR delivery
([task delivery](task-delivery.spec.md)), credentials held by the gateway, lease
fencing, heartbeat, cancel and fail.

## 3.4 Engines

| Engine | Decision |
| --- | --- |
| Claude harness on `anthropic` | Supported (3.3). |
| Codex, Cursor, Copilot, custom, local harness | Not supported. The flag value is refused at start (3.1). A Claude-flag claim leased to another engine falls back with `engine_not_claude`. |

## 3.5 What is recorded

The harness model selection of every assigned claim carries the arm in its
`explanation` ([model observability](model-observability.spec.md)):
`single-layer experiment <id>: arm=<arm>`, plus ` assigned=<arm> fallback=<reason>`
when it fell back. The attempt's `model-policy-attempt-<n>.json` artifact carries
the same decision under `experiment`. Usage, transitions and `pr_opened` are the
existing records ([account usage](account-usage.spec.md),
[fleet health](fleet-health.spec.md)). There is no migration.

## 3.6 Assignment protocol

`assign` takes a JSON list of candidate tasks (`id`, and `stratum` or `difficulty`),
an experiment id and a seed. With `--size` (default 20) it picks that many
candidates, keeping each stratum's share of the pool by largest remainder. Then it
alternates arms within each stratum in a hash order (`sha256(seed, experiment,
task id)`), so every stratum is balanced to within one task. The same inputs always
give the same file. Candidates should be comparable coding tasks that are not yet
claimed. The operator picks them and fixes the seed before the first claim.

## 3.7 Measures

`export` reads a copy of the database read-only. It writes one record per claim,
whose window runs to the task's next claim, because `attempt` restarts after a
retry. A record holds the end transition, the arm and fallback from the marker,
tokens and cost, and the first `pr_opened`. `analyze` reports, per **assigned** arm
(intention to treat: a fallback attempt stays in its assigned arm, and is counted in
`attempts_off_arm` and `fallbacks`):

- **Success.** Task success over finished tasks, attempt success
  (`task.complete` over ended attempts) and attempts per claimed task. A task or
  attempt still in flight is neither success nor failure.
- **Tokens and cost per succeeded task.** Summed over all of the task's attempts.
  Usage is unknown when an attempt has no usage row, a `not_reported` row, or a null
  value. An unknown task is excluded from the figure and counted in `unknown`, never
  taken as 0.
- **Time to pull request opened.** From task creation, and from the first claim, to
  the first `pr_opened`.
- **Time to merged pull request.** Only from a forge export `{pr_url: merged_at}`
  given with `--merged`. Argus does not record merge time, and it is never inferred
  from `pr_opened` or task success.

Time measures report the observed durations (n, median, mean) and the
right-censored count (no PR, or no merge yet). When anything is censored the
observed median is flagged `median_biased_low`. Balance per stratum is reported
with the arms.

## 3.8 Way back

Unset `ARGUS_SINGLE_LAYER` and restart the host: every claim then runs the default
path. Removing a task from the assignment file takes it out at its next claim.
Nothing is stored that needs undoing. The markers stay in `explanation` as history.

# 4. Open Questions

## 4.1 Not yet known

- Whether single-layer changes success rate, tokens per succeeded task or time to a
  merged PR. That is the experiment's question, and no run has been made.
- Whether 20 tasks are enough to tell the arms apart. At this size only a large
  difference is visible. A small or mixed one means "no decision", not "no
  difference".

## 4.2 Known limits

- The `pr_opened` relay depends on the model naming the PR URL. A PR opened and
  never named is censored, which biases the single-layer arm's time to PR. The
  analysis cannot tell this apart from a missing PR.
- The delegated path also has the delegate's tracker, which matches the repository
  as a prefix. The channel relay used by both arms matches it exactly. The two
  emitters can differ on the same output, and the tracker without an API token
  writes only to its local file and stderr.
- The harness model differs between the arms by design. The experiment compares
  the two designs, not models.

# 5. Constraints

- No real model call, paid run or network access in any test. The fake-API tests
  never reach a provider.
- `export` opens the database read-only and is meant for a copy, never the live
  preview database.
- The marker and artifact hold ids, arms and reason codes only, never a secret or
  prompt text.
- No new required service and no new table: single-node rules hold
  ([single node](single-node.spec.md)).

# 6. Implementation Consequences

- `argus-single-layer.ts` holds the pure parts: flag, assignment, eligibility,
  harness selection, prompt lines and PR URL matching. `argus.ts` calls it at
  claim, model preparation, prompt rendering and reply delivery.
- `model-policy.d.mts` declares `implementationScope`. `install.sh` and
  `provision.sh` ship and run the new test.
- If the result favours single-layer, [engines](engines.spec.md) and the draft's
  P7 change in a new spec PR. If it does not, the arm and the flag are removed.
  Either way this spec is then rewritten or deleted.

## Verification boundary

1. **Unit and fake-API tests (done).** `argus-single-layer.test.ts` covers the flag,
   the assignment, each eligibility rule, the harness model, and a prompt identical
   to today's with the flag off. Against a fake Argus API and gateway, it also
   covers a single-layer claim (implementation tier, marker, `pr_opened` once), a
   stale lease on `pr_opened` (abandoned; no complete or fail), cancel through a
   rejected heartbeat, `FAIL` to `/fail`, a delegated arm and an unlisted task
   identical to flag-off, and the fallbacks `executor_model_pinned`,
   `no_model_policy` and `engine_not_claude`. `test_single_layer_ab.py` covers
   assignment determinism and balance, export claim windows and unknown usage on a
   migrated database, and the counting rules on synthetic fixtures.
2. **Real host (not done).** On a NanoClaw host with the flag on, the first claim of
   each arm records its marker and artifact. The operator checks both before
   letting the rest run (runbook, step 3).
3. **The experiment (not done).** About 20 tasks, run per the runbook. Only this
   level produces a result.

Levels 1 and 2 prove the plumbing, not the effect. Mocked tests are not results.
