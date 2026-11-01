# Model-Facing Evals

# 1. Purpose and Scope

Much of Argus's behaviour is text a model reads: MCP tool names, descriptions
and schemas (`apps/api/src/argus_api/mcp_server.py`), the orchestrator skill
(`skills/argus-orchestrator/SKILL.md`), the worker instructions
(`mcp_server.INSTRUCTIONS`) and tool results such as `_describe()` errors. Unit
tests show that a tool returns the right payload, not that a model picks the
right tool, passes the right arguments or recovers from an error.

The top-level `evals/` package runs a model against that text and scores what it
does. It covers two evals:

- `orchestrator-tools`: given an inbox state, does the orchestrator read before
  it acts, avoid mutating tools it does not need, and stop when done?
- `worker-protocol`: does a worker claim, heartbeat, report progress and finish
  in order, and react to a steer, a draining state or a lost claim?

Layout: `evals/<name>/run.py`, `cases/*.json`, `README.md` (what it measures,
how to run, cost). Shared code lives in `evals/*.py`.

# 2. Non-Goals

- Benchmarking engines or models against each other.
- Any eval that needs a live Argus, a database or a live worker node.
- Running evals in CI.
- Testing the tools' own behaviour: that stays in `apps/api/tests`.

# 3. Decisions Made

- **Import, never copy.** `evals/source.py` builds the tool list from the MCP
  server (as listed to the role's scope), the orchestrator prompt from
  `INSTRUCTIONS` plus `ORCHESTRATOR_SKILL`, and the worker prompt from
  `INSTRUCTIONS`. No eval holds its own copy of that text. There is no separate
  worker prompt module today; `INSTRUCTIONS` is the worker text the server hands
  to every client.
- **Cases are data.** One JSON file per case: `id`, `source` (task, PR or
  incident; placeholders are allowed), `prompt`, `world` (recorded responses),
  `control`, and `checks`: `expect` and `forbid` call patterns (tool plus
  required arguments; `"*"` means any value), `in_order`, `max_calls`, and an
  optional `rubric`.
- **Real cases only.** A case reproduces a real bad experience or guards a real
  good one, and is added before the fix. It must fail on the old text.
- **Controls.** Each eval has ordinary-work cases (`"control": true`) where the
  obvious action is right, to catch a fix that overcorrects.
- **Fake world.** `evals/world.py` serves scripted rules per tool (optionally
  `when` args match, `times` uses). Arguments are validated against the tool's
  schema; an unknown or unrecorded call returns an error. No network, no Argus.
- **Samples.** Each case runs `--samples` times (default 5). A case fails when
  half or more of its samples fail. Not stopping within `max_turns` fails the
  sample.
- **Baseline.** `--baseline LABEL` compares with an earlier run of the same
  eval on the same model (a different model is refused) and reports regressed
  and fixed cases. There is no absolute score.
- **Model boundary.** Evals call an injectable `ModelClient`
  (`evals/model.py`); the client reports each call's cost. The CLI takes
  `--client module:factory` and passes `--model` to the factory. Tests use a
  fake client.
- **Output.** `--out LABEL` (required) writes `rows.jsonl` (one row per sample:
  calls, failures, final text, cost) and `summary.json` to
  `evals/<name>/runs/LABEL/`, which `.gitignore` excludes. A short summary with
  total cost is printed.
- **CI.** `make check` runs ruff, `ty` and the unit tests of the graders, the
  stub world, the runner and the case files (every tool a case names must exist
  and be listed to the role). It never calls a model.
- **Rules for agents.** A change to tool descriptions, schemas, tool results,
  the persona or worker instructions runs the evals that cover that text. A fix
  for a model-behaviour bug adds a failing case first. Fixtures hold no user
  data: placeholder paths and repositories only.

# 4. Open Questions

Carried from the [draft](../drafts/2026-10-31.model-facing-evals.draft.md):

- **Which models form the baseline, and is the local model included?** Open.
  The runner takes any model name; the choice waits for a first real run.
- **Harness or direct API?** Decided for now: call the model API directly with
  the tool list (cheaper and steadier). Open: a provider client is not shipped
  here, so `--client` must name one; running through each engine's harness stays
  a later option.
- **Who grades free-form output?** Decided for now: a rubric prompt answered by
  the same `ModelClient` (the first line must be `PASS`), its cost counted in
  the run. Open: whether a person with the new output hidden should grade
  instead.
- Open: the shipped cases use placeholder sources; real incident references
  replace them as cases are confirmed to fail on the old text.

# 5. Constraints

- No new required service or dependency; `jsonschema` and the MCP SDK are
  already in the workspace.
- Fixtures contain no user data: placeholder paths and repositories only.
- Evals never run in CI and never run without an explicit `--out` and `--client`.
- No sleeps, polling or retries in the runner.

# 6. Implementation Consequences

- `evals/` is included in `ty`, `ruff` and `pytest` (`pythonpath` gains `.`).
- `runs/` is gitignored.
- Run: `uv run python evals/<name>/run.py --out LABEL --model MODEL --client
  module:factory [--baseline LABEL] [--case ID] [--samples N]`.

## Verification boundary

- Commands: `uv run pytest evals`, `make check`.
- Verified (unit): grading, stub world, runner with a fake client, summary and
  baseline logic, case files against the real tool list.
- Not verified: any real model run. No case has been shown to fail on old text
  yet, and no provider client exists.
