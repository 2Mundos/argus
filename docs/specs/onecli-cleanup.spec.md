# OneCLI cleanup: vault entries of terminated NanoClaw instances

# 1. Purpose and Scope

Every NanoClaw instance creates its own OneCLI agent (named from
`instances.agent_name`, a mythology word assigned at reserve — or, for rows that
predate that column, the legacy `<pool>-<first 8 of the instance id>`), keyed by
its NanoClaw agent group id, and for each account it leases, a secret named
`argus-<agent group id>` plus a suffix
([account selection](account-selection.spec.md)). Nothing removes them when the
instance ends. On one host this reached about 240 agents and 750 secrets, most
holding credentials that were rotated long ago. A OneCLI agent left in `all`
secret mode injects every one of them, which once sent a stale Anthropic key on
every orchestrator request (fixed by the orchestrator's own lease,
[internal orchestrator](internal-orchestrator.spec.md) 3.1).

This spec adds an opt-in, dry-run-first loop in the API that deletes the OneCLI
agent and the secrets of instances stored as `terminated`.

# 2. Non-Goals

- No cleanup for an instance that is not stored as `terminated`. Being absent
  from a live listing, a lost agent or an old age is never enough.
- No deletion of a OneCLI agent or secret whose name does not follow the
  patterns above, including secrets of agents OneCLI no longer lists.
- No change to how NanoClaw creates, assigns or rotates secrets.
- No new service and no new dependency. OneCLI stays optional: without its URL
  the loop does not run.

# 3. Decisions Made

## 3.1 Settings (off by default)

| Variable | Default | Meaning |
| --- | --- | --- |
| `ARGUS_ONECLI_CLEANUP_URL` | unset | OneCLI API base reachable from the API process (for example `http://127.0.0.1:10254`). Unset: the loop does not run |
| `ARGUS_ONECLI_CLEANUP_API_KEY` | unset | Bearer token for that API, when OneCLI requires one |
| `ARGUS_ONECLI_CLEANUP_DRY_RUN` | `true` | Only report; `false` lets it delete |
| `ARGUS_ONECLI_CLEANUP_INTERVAL_S` | `3600` | Pause between runs |

This is separate from `ARGUS_NANOCLAW_ONECLI_URL`, which is the address the
instances use (often a Docker network name the API host cannot resolve).

## 3.2 What a run does

1. Read the instances stored as `terminated` and every other instance (one read
   transaction).
2. List OneCLI agents and secrets (`GET /api/agents`, `GET /api/secrets`).
3. A OneCLI agent is **stale** when its name equals the resolved display name
   of a terminated instance (`instances.agent_name`, or legacy
   `<pool>-<id[:8]>` when that column is null) and of no instance in any other
   state. Its secrets are those named `argus-<identifier>` or starting with
   `argus-<identifier>-`, where `identifier` is that agent's identifier.
4. Dry run: report only. Otherwise delete each stale agent's secrets
   (`DELETE /api/secrets/{id}`), then the agent (`DELETE /api/agents/{id}`). A
   failed delete is counted and retried on the next run; the run goes on.
5. Record one `onecli.cleanup` event (actor `system`, subject `system`) with
   `dry_run`, the counts (`agents`, `secrets`, `failed`) and up to 50 agent names.
   No secret value, identifier list or credential is ever read or logged; only
   names and ids from the listings.

Network calls happen outside any write transaction; the event is written after.

## 3.3 Safety

- The match is by exact agent name, so an agent of a running instance is never
  touched, and two instances whose names collide are both left alone while
  either is not terminated.
- An agent with no matching terminated instance is never deleted.
- A listing error ends the run with no deletion.

# 4. Open Questions

- Whether OneCLI deletes an agent's secret assignments with the agent. The loop
  deletes the secrets first, so it does not depend on it.

# 5. Constraints

- [Single node](single-node.spec.md): one in-process loop, an optional
  integration that is off by default.
- All writes go through `Database.write()`; no network call inside it.

# 6. Implementation Consequences

- `apps/api/src/argus_api/fleet/onecli_cleanup.py`: the plan (pure) and the loop.
- `config.py`: the four settings. `main.py`: start the loop when the URL is set.
- Operators enable it with the URL and a first dry run, read the
  `onecli.cleanup` event, then set `ARGUS_ONECLI_CLEANUP_DRY_RUN=false`.

## Verification boundary

- Commands: `uv run pytest -q apps/api/tests/test_onecli_cleanup.py`; `make test`
  runs the whole suite.
- Verified: the plan against synthetic instance rows and listings, and a run
  against a mock OneCLI HTTP API (dry run, real run, a failed delete).
- Not verified: a real OneCLI, whether it cascades secret assignments on agent
  delete, and any deployed node.
