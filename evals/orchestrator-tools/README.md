# orchestrator-tools

**Measures.** Given an inbox state, whether the orchestrator reads before it
acts, uses the right tools and arguments (`request_id`, `expected_version`),
avoids tools it must not use (worker tools, bulk answers, answering a `human`
decision), and stops when done. Cases are in `cases/`; the control
(`control-simple-question`) is ordinary work where answering is correct.

**Text under test, imported.** Tools: the MCP server's list for the orchestrator
scope. System prompt: `mcp_server.INSTRUCTIONS` plus
`mcp_server.ORCHESTRATOR_SKILL` (`skills/argus-orchestrator/SKILL.md`).

**Run.**

```bash
uv run python evals/orchestrator-tools/run.py --out my-run --model MODEL \
  --client package.module:factory [--baseline earlier-run] [--case ID] [--samples 5]
```

Results land in `evals/orchestrator-tools/runs/my-run/` (gitignored):
`rows.jsonl` per sample and `summary.json`. `--baseline` must be a run on the
same model. `--client` names a function `factory(model) -> ModelClient`
(`evals/model.py`).

**Cost.** Cases x samples model calls (a few turns each, plus one grader call for
rubric cases), roughly 5 cases x 5 samples x ~6 turns with a ~35k-token system
prompt. Use the cheapest model that exercises the text. The summary prints the
exact cost reported by the client.

Add a case before fixing a model-behaviour bug; see
[the spec](../../docs/specs/model-facing-evals.spec.md).
