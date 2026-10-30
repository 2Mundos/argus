# Failure classes

# 1. Purpose and Scope

Every failed attempt is retried by one rule today (`failures + 1 < max_attempts`),
whatever went wrong. A failure that will repeat identically (no repository, an
executor that cannot log in) burns the whole attempt budget, and a model that
said the task cannot be done is asked again twice. Production data (2026-10-16
to 2026-10-24, [worker efficiency](../drafts/2026-10-24.worker-efficiency.draft.md)
P6 and the preflight half of P2): about 20 failures reported "no checkout in
/workspace" and were retried into the same empty workspace, and an outer `catch`
in the channel reported every error after the account step as "repository
credential unavailable".

This spec:

1. gives every failure reason one class, in one table;
2. lets the class pick the retry action;
3. adds a deterministic channel preflight that blocks with a typed reason before
   any model work, and replaces the blanket `catch` with one reason per step.

# 2. Non-Goals

- No owned workspace or worktree (P2's workspace half) and no session resume
  (P4): the automatic resume of 3.2 requeues the task into a new attempt and a
  new session; it does not resume the old session's conversation.
- No change to admission (`quota`, `account_unavailable`;
  [account selection](account-selection.spec.md)) or to difficulty escalation
  ([task difficulty](task-difficulty.spec.md)).
- No classification of free-form error text on the server beyond what Fleet
  already does.
- No new service; one nullable column.

# 3. Decisions Made

## 3.1 One table of classes

`argus_core.tasks.FAILURE_CLASS` maps each reason to a class. Fleet, the fail
service and the dashboard read it; nothing else maps reasons.

| Class | Reasons |
| --- | --- |
| `admission` | `quota`, `account_unavailable` |
| `environment` | `env_*` block reasons (3.3), `worker_env` (3.4), `env_oom` (3.2; the promoted, pool-level block only, not a per-task reason) |
| `transient` | `provider_failure`, `channel_error`, `runtime_switch_failed` |
| `session` | `session_lost`, `stalled`, `timeout`, `lease_expired`, `agent_ended`; fact `oom_kill` (the session container was OOM-killed) |
| `task` | `worker_fail`, `context_overflow`, `verification_failed` |

A reason not in the table is `task`. Fleet's `classify_failure` keeps its
reasons and adds `failure_class` to each failure row and a count per class.

## 3.2 The class picks the action

| Class | Action | Spends an attempt |
| --- | --- | --- |
| `admission` | unchanged: requeue or `awaiting_quota` | no |
| `environment` | `blocked` with the `env_*` reason; inbox row; a manager unblocks | no |
| `transient` | requeue after a backoff (below) | yes |
| `session` | requeue once after a confirmed exit; a further automatic requeue needs progress since the last automatic resume, else the task fails with the cause | yes |
| `task` | escalating reasons: requeue one difficulty up, as today; `worker_fail`: `blocked` as `worker_gave_up` | yes |

- **Backoff.** A `transient` fail that requeues sets `tasks.retry_at` to
  `tx.now + min(60 · 2^(failures − 1), 900)` s, `failures` counted after this
  fail (60 s, 120 s, 240 s, …, at most 15 min). `claim()` skips a ready task
  whose `retry_at` is after `tx.now`; any other transition clears `retry_at`.
  Migration `0047_task_retry_at.sql`.
- **Gave up.** A `worker_fail` that leaves attempts in the budget blocks the task
  as `worker_gave_up` instead of requeueing it, with the worker's text as `error`.
  The inbox shows it like any block; unblock retries it, cancel ends it. A
  `worker_fail` on the last attempt fails the task, as today.
- **Session resume.** `session_lost` and `agent_ended` are the unexpected exits
  and follow the rule below. `stalled`, `timeout` and `lease_expired` are not
  exits the host confirmed, and keep today's rule: requeue at once.
- **Automatic resume rule** (for `session_lost` and `agent_ended`):
  - A **confirmed exit** means the old container is confirmed gone. No new check
    is added; the existing evidence is reused:
    - `session_lost`: the channel sends it only after its session listing has
      shown no container for the attempt for 90 s
      ([worker lifecycle](worker-lifecycle.spec.md) 3.8). That check is the
      confirmation.
    - `agent_ended` (the reaper ends a lost or departed agent): confirmed only
      when the host's last lease observation for this attempt, taken after its
      claim, reported the session `gone` (stall visibility 3.7). Without it, the
      exit is not confirmed and keeps today's rule (requeue at once).
    - `stalled`, `timeout` and `lease_expired` are never confirmed exits.
  - The first confirmed exit of a task requeues it, as long as `max_attempts`
    allows. It records `task.auto_resumed` (new event; actor: the failing agent,
    or `system` for the reaper; data `{attempt, reason}`, no error text) in the
    fail transaction.
  - A later confirmed exit requeues only if **progress** was recorded for the
    claimed attempt after the latest `task.auto_resumed`. Progress is a
    `task.progress` event of this task whose `data.attempt` is the claimed
    attempt (attempt numbers only grow within an epoch, and a late write of an
    earlier attempt is refused). A milestone (`data.kind` set) is a
    `task.progress` event and counts. Nothing else counts: heartbeats
    and their `activity_at`, live log lines, failure notices, the
    `progress_message` column (the server writes it too), and progress of
    another task or an earlier attempt.
  - Otherwise the task fails with the cause: the attempt is spent, the task is
    `failed` whatever is left of `max_attempts`, and its `error` is the exit's own
    text (for `session_lost` the exit code and the redacted stderr excerpt the
    channel sends, at most 2 KiB, with the `stderr-tail` artifact as the full
    tail; the server keeps at most 2,048 characters). The `task.fail` (or
    `task.expire`) event carries `auto_resume: "no_progress"`. A retry is the
    operator's act.
  - `POST /tasks/{id}/retry` and an operator's unblock (any actor but `system`:
    the unblock route, an answered decision, a manager's runtime release) start a
    new **recovery epoch**: events before the latest such `task.retried` or
    `task.unblock` are ignored. Unblocks the server does on its own (a quota
    wake, a finished runtime build) do not, so a crash loop that passes through
    a quota wait stays bounded.
  - The rule is checkable from the events: `task.auto_resumed`, `task.progress`,
    `task.retried` and `task.unblock` for one task, no counter column. Each lookup is one seek on the partial index
    `events_task_kind (subject_id, kind, id) WHERE subject_type = 'task'`
    (migration `0057_events_task_kind.sql`), newest first or `LIMIT 1`; no scan of
    the event log.
  - The fail is fenced as every worker write (agent, attempt, state): a repeated
    or late fail of the same attempt is a `409` and records nothing, so one exit
    records at most one `task.auto_resumed`.
  - **Plain replies (decided).** The channel posts every non-empty reply without
    a directive as a progress note (`POST /tasks/{id}/progress`, coalesced to one
    per 10 s), so a completed turn with text is progress through that
    `task.progress` event. A turn with no text, or a note still coalescing when
    the claim ends, posts nothing and is not progress.
- `max_attempts` keeps its meaning: the cap on attempts that count.
- **OOM.** `env_oom` blocks after 2 consecutive OOM-killed sessions on one pool.
  Before that, an OOM-killed session is a `session` attempt as today, with the
  `oom_kill` fact recorded on the failure row.

## 3.3 Channel preflight

After the account and repository lease, and before injection, the
channel runs these checks in order. The first one that fails calls
`POST /tasks/{id}/block` with its reason and a short `error`, then finishes the
task. No model session starts and no attempt is spent.

| Check | Reason |
| --- | --- |
| The task needs `repository:write` and its project has a repository URL | `env_repository_unconfigured` |
| The repository lease is taken and applied (today's step) | `env_repository_unavailable` |
| A preflight session (3.3.1) proves an authenticated write credential for exactly this repository, within its deadline, and is torn down | `env_repository_unreachable` |
| The task workspace has a place: the agent group dir is known and the task has a repository URL | `env_workspace_unconfigured` |
| The task workspace can be created and read (no `EACCES`, `EPERM` or `EROFS`) | `env_workspace_permission` |
| The task workspace is a real directory under the workspaces root and a git repository | `env_workspace_invalid` |
| Its `origin` is the task's repository URL, without credentials | `env_workspace_remote_mismatch` |
| A checkpoint is expected but missing, incomplete, or from another node (no proven transport) | `env_checkpoint_unavailable` |
| A checkpoint's hashes, identity, paths or permissions fail validation | `env_checkpoint_invalid` |
| The workspace already holds work that differs from the checkpoint | `env_checkpoint_conflict` |
| Another holder has the task's checkpoint lease | `env_checkpoint_busy` |
| The work exceeds the checkpoint size limit | `env_checkpoint_limit` |
| Its HEAD is the task branch | `env_workspace_branch_mismatch` |
| A new workspace stays within `ARGUS_TASK_WORKSPACE_MAX` | `env_workspace_limit` |
| A required task input is listed, shared and downloadable ([task inputs](task-inputs.spec.md) 3.6) | `env_input_unavailable` |
| A required task input has a safe name and destination, and its size and SHA-256 match after the retry | `env_input_invalid` |
| The task inputs stay within the per-input, per-task and file-count limits | `env_input_limit` |

- The workspace checks run only for `repository:write` tasks, after the
  repository checks; [task workspace](task-workspace.spec.md) 3.4 defines them.
- The input checks run for every task with a required input, before the
  workspace is prepared and before any model runs
  ([task inputs](task-inputs.spec.md) 3.4).

### 3.3.1 Repository preflight session

The NanoClaw host has no OneCLI HTTPS proxy: only a session container is configured
with the proxy and its CA, so a probe run in the host process can never reach the
forge (it fails with `OneCLI HTTPS proxy is not configured`). The repository check
therefore runs in a bounded, ephemeral, model-free session:

- **Same session machinery.** A session of the Argus agent group, from the same image
  and the same driver, composed by NanoClaw's own `composeSessionSpec` with the gateway
  lease taken through `ensureGatewaySession` (the worker path's admission gate;
  `install.sh` exports it). The composed security profile, user mapping, limits, network
  and the gateway's mounts are kept exactly; the driver realizes them or refuses the spec
  (`assertDriverSupportsSecurityProfiles`, then the driver's own checks), and a refusal is
  `env_repository_unreachable`. Only the worker mounts are left out and one read-write
  mount, `/preflight`, is added. A cold start works because OneCLI's container config
  belongs to the agent group, not to a session.
- **No model, no credential.** PID 1 is the credential-free helper (`argus-github-api` or
  `argus-gitea-api`, by the repository's host) running `repo-preflight`; no model runner or
  mailbox starts. Its allowlist is exactly the task's repository. OneCLI injects the
  credential at the proxy; none is in the host's environment, argv or logs.
- **Effect on the group.** The preflight lease only reads: OneCLI's `ensure` registers the
  agent idempotently and reads its container config, and releasing it aborts that lease's
  own controller. The repository lease, the OneCLI assignments, the task workspace and any
  live worker session (its lease, availability monitor and container) are not touched. A
  gateway outage is reported to the preflight through the same shared monitor.
- **One deadline.** Every step (agent group lookup, gateway lease, composition, prepare, start,
  status) shares one 90 s deadline, and every teardown step (stop, reap by key, gateway release,
  late cleanup, listing) is bounded to 10 s, so a probe ends within 165 s
  (`PREFLIGHT_TOTAL_MS`). A step that outlives its bound keeps running; whatever it produces
  late (a lease, a prepared handle) is released or stopped, and a container created by a
  prepare that never returned is found by its key and removed. Failing to stop the container,
  release the lease, or see it gone is `env_repository_unreachable`, never a pass. No model
  starts on any of these paths: injection happens only after the preflight returns.
- **The verdict.** `repo-preflight` writes a verdict file under `/preflight` as its last act.
  The host reads it without trusting the directory: the directory and file must be plain
  (not symlinks, one link, ours), the open refuses a symlink, and the size is bounded; the
  helper creates the file exclusively with mode 0600 and never follows a planted link. The
  result directory is owned by the container's mapped user (or chowned to it when the host
  is root); a user that cannot write it fails closed.
- **What the task is told.** Nothing the container wrote is passed on as text. A failing child
  is reported as a closed reason: the helper's error code if it is one of its fixed codes, else
  "of an unknown kind", else "no verdict was written". Provider and repository fields in the
  verdict are compared, never echoed. Errors from the driver and OneCLI are printable-only,
  credential-scrubbed and cut to 160 characters, and the channel redacts and bounds the block
  `error` again.
- **Startup order.** NanoClaw's `index.ts` starts the channel adapters (the Argus claim loop)
  before it calls `adoptRunningSessions()`, which stops every listed session that has no
  database row, as a preflight session is. `install.sh` therefore wraps adoption and exports
  `startupAdoptionDone` (`patch-startup-adoption-gate.py`, fail-closed on an upstream change).
  The claim loop claims nothing until adoption succeeds (if the hook export is missing it logs
  and claims; if adoption throws, claiming stays blocked with periodic visibility). A preflight
  creates no lease, directory or
  container before it settles, inside its deadline. A test pins `index.ts`'s order, the hazard
  and the gate against the real adoption.
- **Who may declare the task blocked.** Only the preflight itself, after its teardown has
  concluded (every step, teardown included, is bounded: `PREFLIGHT_TOTAL_MS`). The channel's
  outer mark (`PREFLIGHT_TOTAL_MS` + 30 s) only logs: the task is never blocked, nor its
  repository lease released, while the preflight session may still exist.
- **Secrets in text.** OneCLI's session proxy URL carries its agent token as userinfo and a
  failed `docker` call prints its argv. URL userinfo and proxy-variable values are redacted
  from everything the runner throws, from the channel's block `error` and from its log.
- **Crashes and outages.** A preflight session is not a worker session: NanoClaw's
  gateway-unavailable stop and its orphan sweep act on registered worker runtimes and never
  see it, so the preflight watches its own lease (an outage reported at any point, including
  as the child ends, fails it). If the host dies mid-preflight, startup adoption stops the
  container (a listed session with no database row is stopped by its own handle, nothing else),
  and the next preflight removes the scratch directory it left: only a direct child of the
  group's session directory named exactly `preflight-<12 hex>`, a real directory (never a
  symlink), unlisted by the driver and older than twice the longest possible preflight. Another
  session's directory cannot match. Residual: until the next host start the orphan container
  keeps running, and until the next preflight its scratch directory stays.
- **What is accepted.** Exit code 0 and an `ok` verdict for the lease's provider and this
  repository with `authenticated`, `write`, `allowlist_refuses_other_host` and
  `allowlist_refuses_sibling` true. The driver reports a non-zero exit as `failed` with
  the code and exit 0 as `stopped`; because Docker's transient `removing` state also reads
  as `stopped`, the host reads the status again before it trusts `stopped`, and a session that
  reads running again keeps being polled until the deadline. A lease of the
  other provider, a repository outside the allowlist, a missing proxy in the gateway
  contribution (empty counts as missing), any other exit, a missing verdict, an unavailable
  gateway or an exceeded deadline is `env_repository_unreachable`. The task is blocked, not
  failed: its leases are released as for any block, its workspace and WIP are untouched, and
  no attempt is spent. Without a configured preflight (`probeRepository`) the check fails
  closed.

**Exactly what each part proves.**

| Evidence | Proves | Does not prove |
| --- | --- | --- |
| `permissions.push` true for the same `owner/repo`, read by the helper through the session's proxy | The credential the gateway attached to that request is authenticated and may write to that repository, as the forge reports it. An anonymous read of a public repository cannot produce it. | That OneCLI's host/path rules stop the token reaching other hosts or repositories; that the token has no broader rights; Git smart-HTTP access (a different secret). |
| `allowlist_refuses_other_host`, `allowlist_refuses_sibling` | The helper's own allowlist policy code refuses another host and a sibling repository (a local check, no network). | Anything about the proxy, OneCLI scoping or the token. Treat as a self-test of the helper in the deployed image. |
| Offline tests with a fake docker CLI | The host's control flow: deadlines, late cleanup, teardown failures, verdict handling, what is accepted. | Forge or gateway behaviour. |
| Container tests (`argus-repo-preflight.docker.test.ts`, set `ARGUS_PREFLIGHT_DOCKER_IMAGE`) against a synthetic CONNECT proxy and forge with a throwaway CA | With a real daemon and driver: the container user writes the verdict, exit codes (13, 15, 137) and removal, the deadline, a planted symlink is not followed, a wrong user fails closed. | Any real forge, real OneCLI, a real token or the production image. |
| The rollout canary ([worker lifecycle](worker-lifecycle.spec.md)) | The real OneCLI account and repository. This is the only evidence of production behaviour. | |

- `env_workspace_missing` is not a preflight check: the channel blocks with it
  when the agent group directory stays missing on the host for 90 s while the
  task is claimed ([stall visibility](stall-visibility.spec.md) 3.7).
- `env_executor_auth` is reserved for an executor credential check. It is not run
  yet: `argus-delegate` only runs inside the session container, which does not
  exist at preflight time (4). Until then a worker whose executor cannot log in
  reports it with `FAIL (environment):` (3.4).
- `BlockRequest` gains an optional `error` (at most 1,500 characters, redacted by
  the channel like the live log), stored as the task's `error`. A worker may block
  only with an `env_*` reason from `ENV_BLOCK_REASONS` or the reasons it sends
  today; anything else is `422`.
- Steps after the preflight fail with their own reason and the real error:
  page inlining and injection as `channel_error` naming the step. The blanket
  "repository credential unavailable" message goes away.
- A blocked `env_*` task shows in the inbox under its project, as blocked tasks
  already do ([MCP operations](mcp-operations.spec.md) 3.2).

## 3.4 The worker can say "environment"

The reply protocol gains `FAIL (environment): <why>` (in the scoped form,
`FAIL on <task-id>: (environment) <why>`). The channel blocks the task
as `worker_env` with `<why>` as `error`, instead of failing it. The task prompt
says to use it only when the workspace, tools or credentials are missing, not
when the work is hard. Any other `FAIL:` is `worker_fail`.

# 4. Open Questions

- Should one `env_repository_*` block hold the project's other ready tasks until
  it is unblocked, or is one cheap preflight per task fine?
- How should the executor credential be probed before the session starts: a
  throwaway container on the session image, or a check NanoClaw runs at session
  start?
- Is blocking on the first `worker_fail` too strict for tasks that would pass on a
  plain retry? Re-check after two weeks against the baseline.
- Is 2 consecutive OOM-killed sessions the right threshold for `env_oom`? Should
  it count per pool or per task, and over what window?

# 5. Constraints

- Single-node, SQLite only; all writes through `Database.write()`, times from
  `tx.now`. No network calls in a write transaction: the channel probes before it
  calls the API.
- Preflight probes are best-effort and bounded by their timeouts. A probe that
  cannot run (the tool is missing) is an `environment` block, not a pass.
- No credentials in `error` or log excerpts.
- English only.

# 6. Implementation Consequences

- Core: `FAILURE_CLASS`, `ENV_BLOCK_REASONS`; `transition` unchanged (the service
  picks `BLOCK` or `FAIL`).
- API: `fail` applies 3.2; `claim` honours `retry_at`; `BlockRequest.error`;
  migration `0047`; Fleet `failure_class`; `make openapi`.
- Channel: preflight steps and reasons, `FAIL (environment):`, per-step errors in
  place of the outer `catch`.
- Dashboard: inbox copy for `env_*`, `worker_env` and `worker_gave_up`; Fleet
  counts by class.
- Amend [worker lifecycle](worker-lifecycle.spec.md) (the fail and block
  paragraphs). Mark P6 and the preflight half of P2 as specified in the
  [worker efficiency](../drafts/2026-10-24.worker-efficiency.draft.md) draft.
- Tests per layer: one per class action, the backoff schedule, each preflight
  reason, and the environment FAIL directive.
- Automatic resume (3.2): `fail` and the reaper's `_expire_where` apply the rule
  in the fail transaction (`_auto_resume`); migration
  `0057_events_task_kind.sql` adds the partial index it seeks; the new event
  `task.auto_resumed`. The channel's `failSessionLost` retries a transient error
  a bounded number of times and logs the state Argus answered.

## Verification boundary

- Commands:
  - `uv run pytest -q apps/api/tests/test_auto_resume.py apps/api/tests/test_dead_session_recovery.py apps/api/tests/test_stall_causes.py apps/api/tests/test_tasks_service.py`
  - `integrations/nanoclaw/provision.sh check` (vitest in a NanoClaw checkout;
    the automatic resume half is `argus-auto-resume.test.ts`).
  - `make check` runs the rest.
- Verified, against a disposable SQLite database, a fake clock and the HTTP routes
  through a test client:
  - a crash loop resumes once, then fails with the cause and `auto_resume:
    "no_progress"`;
  - a progress note or a milestone of the claimed attempt earns one more resume;
    heartbeats, live log lines, another task's progress and an earlier attempt's
    progress do not;
  - retry and an operator unblock start a new epoch, a `system` unblock does not;
  - `stalled`, `timeout`, `lease_expired`, the quota fallback and an environment
    block are unchanged;
  - `agent_ended` follows the rule only after a `gone` observation dated after
    the claim;
  - concurrent and repeated fails record one `task.auto_resumed`, and a stale or
    lost holder is refused;
  - the error is capped and the event carries no error text;
  - `EXPLAIN QUERY PLAN` shows each read is one seek on `events_task_kind`.
  - The channel tests (fake runtime, scripted Argus) cover one report with the
    attempt and a redacted error, the bounded retry, no retry after a `409`, and a
    plain reply posted as progress before the exit.
- Not verified: real Docker sessions dying, a real NanoClaw host lost mid-claim,
  and any deployed node.
