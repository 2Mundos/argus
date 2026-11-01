# worker-protocol

**Measures.** Whether a worker claims, heartbeats, reports progress and finishes
in order with the task's `attempt`; acks a steer after injecting it; stops on a
rejected lease or `409`; and calls `leave` after finishing when draining. The
controls (`claim-work-finish`) are the plain happy path.

**Text under test, imported.** Tools: the MCP server's list for the worker
scope. System prompt: `mcp_server.INSTRUCTIONS`, the only worker text the repo
serves to a client.

**Run.**

```bash
uv run python evals/worker-protocol/run.py --out my-run --model MODEL \
  --client package.module:factory [--baseline earlier-run] [--case ID] [--samples 5]
```

Results land in `evals/worker-protocol/runs/my-run/` (gitignored). The fake
world serves recorded responses; no Argus or worker is involved.

**Cost.** Cases x samples model calls, short transcripts and a small system
prompt, so cheaper than `orchestrator-tools`. The summary prints the cost the
client reports.

See [the spec](../../docs/specs/model-facing-evals.spec.md).
