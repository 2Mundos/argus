# NanoClaw integration

An Argus worker channel for [NanoClaw](https://github.com/qwibitai/nanoclaw): it
makes a NanoClaw agent group claim tasks from Argus, work on them, and report
back. See [the research note](../../docs/research/2026-10-14.nanoclaw-integration.research.md)
and [worker lifecycle spec 3.8](../../docs/specs/worker-lifecycle.spec.md).

## What it does

The channel runs in the NanoClaw **host** process; the agent container never
holds an Argus token.

| Direction | Mechanism |
| --- | --- |
| Claim | registers with a bootstrap token, long-polls `/api/tasks/claim`, injects the task (spec, acceptance, pinned knowledge page bodies) as an inbound message |
| Liveness | heartbeats, renewing the lease on the task it holds; stops claiming when Argus reports `draining`; leaves Argus on shutdown |
| Reply | plain message → progress note (at most one per 10 s, coalesced); `COMPLETE: …` → complete; `FAIL: …` → fail; `ASK: …` → ask a question (the task waits for an answer) |

One task at a time per channel. A 409 from Argus (lease lost, attempt
superseded) drops the reply and the channel moves on.

## Install

```bash
integrations/nanoclaw/install.sh /path/to/nanoclaw      # copies files, appends one import
cd /path/to/nanoclaw
pnpm run build && pnpm exec vitest run src/channels/argus.test.ts
pnpm exec tsx scripts/init-argus-agent.ts --agent-name "Worker"   # agent group + wiring
```

Then start the NanoClaw host with the Argus environment (what an Argus `plain`
runtime container receives):

```bash
ARGUS_URL=http://argus:8000 ARGUS_BOOTSTRAP_TOKENS=argus_boot_… \
ARGUS_CAPABILITIES=python,git pnpm start
```

Without `ARGUS_URL` the channel is simply off.

## Not done yet

- Account leasing and the OneCLI credential switch (verified to work on a running
  process; not wired into the channel).
- Several agents per NanoClaw host (`agents_per_instance > 1`).
- Packaging as an Argus `nanoclaw` runtime image.
- Replies are matched by prefix, which an agent can get wrong; an MCP tool set
  would be sturdier but would put an Argus token near the agent.
