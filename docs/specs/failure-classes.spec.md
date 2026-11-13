---
status: partial
created: 2026-10-24
updated: 2026-11-13
---

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
  already does. One fixed text is read: the channel's `channel_error` fail that
  quotes Argus's `(422 deliverable_missing)` refusal marks a task as eligible for
  the delivery reconciliation ([deliverable](task-deliverable.spec.md) 3.8). It
  changes no class and no action.
- No new service; one nullable column, and two counters for OOM (3.2.1).

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
  The inbox shows it like any block; unblock retries it, cancel ends it, keeping the block reason and naming the actor in `progress_message` and the `task.cancel` event. A
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
      when the host's lease observations for this attempt, after its claim, saw
      the session `gone` without a break for the same 90 s grace, up to the last
      one (stall visibility 3.7). Each heartbeat's `gone` starts or extends
      `tasks.session_gone_at` (migration `0059_task_session_gone_at.sql`); any
      other session state, or a renewal without an observation, clears it, and a
      claim or an expiry resets it. One `gone` listing (a container restart, a
      gap) is not a confirmation. Without it, the exit is not confirmed and keeps
      today's rule (requeue at once).
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
    tail; the server keeps at most 2,048 UTF-8 bytes, cut on a character
    boundary). The reaper's own `error` is built from ids, times and enum names,
    never session output. The `task.fail` (or
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
    (migration `0058_events_task_kind.sql`), newest first or `LIMIT 1`; no scan of
    the event log.
  - The fail is fenced as every worker write (agent, attempt, state): a repeated
    or late fail of the same attempt is a `409` and records nothing, so one exit
    records at most one `task.auto_resumed`.
  - **Plain replies (decided).** The channel posts every non-empty reply without
    a directive as a progress note (`POST /tasks/{id}/progress`, coalesced to one
    per 10 s), so a completed turn with text is progress through that
    `task.progress` event. A turn with no text, or a note still coalescing when
    the claim ends, posts nothing and is not progress.
- `max_attempts` keeps its meaning: the cap on counted failures. `attempt`
  counts claims and is bumped on every claim; `failures` counts the failures
  that count (a worker fail or an expiry) and is what `max_attempts` limits, so
  `attempt` may exceed `max_attempts`. These paths claim again without spending
  `failures`: a provider quota pause, an admission failure, a runtime still
  preparing, an operator requeue or release, and a review rejection. A retry of
  a failed task resets `failures` to 0 and `attempt` to 0, so the next claim is
  attempt 1. The two values are never compared with each other.
- **OOM.** `env_oom` blocks after 2 consecutive OOM-killed sessions on one pool.
  Before that, an OOM-killed session is a `session` attempt as today, with the
  `oom_kill` fact recorded on the failure row. 3.2.1 defines it.

### 3.2.1 OOM evidence and the `env_oom` promotion

- **Evidence.** Only the container runtime's own exit state of the exact session
  container the channel saw for this claim counts. When the channel confirms a
  `session_lost` (3.2), it reads `docker inspect` of that container (by the name
  it listed, `seenSessionId`) as structured data: `State.Running`,
  `State.ExitCode`, `State.OOMKilled` and `Name`. The session was OOM-killed only
  when the container is not running, `OOMKilled` is the boolean `true`, the exit
  code is exactly `137`, and the inspected `Name` is the container the channel
  saw. Anything else is no evidence: exit 137 alone (a `SIGKILL`, a `docker kill`,
  a `docker stop` past its grace), a stop Argus or the channel made (recycle,
  resend, an ended claim: no `session_lost` is sent), any other exit code,
  `OOMKilled` false or missing, a running container, an inspect that fails, times
  out or answers unparsable output, and another container's state. Log and stderr
  text, model output and error prose never set it, whatever they say ("Out of
  memory", "OOMKilled"). Only container-cgroup kills are seen: a pool without a
  memory limit, or a host-wide OOM kill, produces no evidence.
- **The fact.** The channel adds `oom_kill: true` to `POST /tasks/{id}/fail` only
  with that evidence; otherwise the field is absent. The server accepts it only
  with `reason: session_lost` (`422` otherwise) and from the lease holder, fenced
  as every fail (agent, attempt, state): a stale, repeated or foreign fail is a
  `409` and records nothing, the streak included. The reaper never sets it. The
  fact is stored on the task as `tasks.oom_kill` (`1` when the attempt that ended
  last was OOM-killed, `0` after any other end of a claim and after a retry) and
  in the `task.fail` or `task.block` event data as `oom_kill: true` with `pool`
  and `oom_streak`.
- **Pool.** The pool is the server's: the failing agent's `agents.instance_id` →
  `instances.pool`. The request names no pool, and a field it adds is ignored. An
  agent outside any pool records the fact but has no streak and never promotes.
- **Streak.** `pools.oom_streak` counts the pool's consecutive OOM-killed session
  exits. Per pool, never across pools; every task on the pool shares it. An
  evidence-bearing `session_lost` fail adds one. Any other end of an attempt by an
  agent of the pool sets it to 0: a `complete`, any other `fail` (a
  `session_lost` without the fact included) and a reaper expiry (`stalled`,
  `timeout`, `lease_expired`, `agent_ended`). Admission fails (`quota`,
  `account_unavailable`), blocks and releases never ran to an outcome and leave
  it. An operator's unblock of an `env_oom` block (any actor but `system`) and a
  change of the pool's `memory_mb` set it to 0; a retry does not.
- **Promotion.** The streak is read and written in the fail's own
  `Database.write()` transaction, after the fence, so concurrent fails on one
  pool are serialized by the single writer: none is counted twice or skipped,
  and at most one decides each step. With the streak after this fail:
  - below 2, the attempt is a `session` exit under 3.2 (spent, the automatic
    resume rule and its progress gate apply unchanged);
  - at 2 or more, when attempts are left (`failures + 1 < max_attempts`), the
    task is `blocked` as `env_oom` (class `environment`) instead: the attempt is
    spent (`failures + 1`), no further one is, and no `task.auto_resumed` is
    recorded. The block happens before the progress gate is read: a human
    decides next. The claim is released as for any block; `error` keeps the
    exit's own text (container, exit code, the redacted excerpt), the
    `stderr-tail` artifact and the checkpoint/WIP are untouched. The `task.block`
    event carries `reason: env_oom`, `cause: session_lost`, `oom_kill`, `pool`,
    `oom_streak` and `error`;
  - on the last attempt the task fails as today: the cap wins over a block, and
    the fact is still recorded and the streak counted.
  The streak stays at 2 or more after a promotion, so the pool's next OOM-killed
  session blocks too, until an operator unblocks or the pool's memory changes.
- **Unblock and retry.** Unblocking an `env_oom` task requeues it with its
  `failures` kept and starts a new recovery epoch (3.2), and resets the pool's
  streak; the next OOM-killed session of the pool is a `session` exit again
  (one automatic resume at most without progress), the one after it blocks
  again. `max_attempts` bounds the whole loop, so it cannot crash-loop; nothing
  bypasses the cap or the progress gate.
- **Visibility.** The task read model carries `oom_kill`, `block_reason: env_oom`
  and the preserved `error`; the inbox shows the block. Fleet health lists each
  pool's `oom_streak` and groups the promoted blocks as `env_oom` (class
  `environment`) next to the `session_lost` fails ([fleet health](fleet-health.spec.md)).

## 3.3 Channel preflight

After the account and repository lease, and before injection, the
channel runs these checks in order. The first one that fails calls
`POST /tasks/{id}/block` with its reason and a short `error`, then finishes the
task. No model session starts and no attempt is spent.

| Check | Reason |
| --- | --- |
| The task's project has no repository URL in the pool map but the task needs write or a configured project checkout | `env_repository_unconfigured` |
| The repository lease is taken and applied, and its OneCLI assignments are confirmed for the preflight's agent (3.3.1) | `env_repository_unavailable` |
| A preflight session (3.3.1) proves read or write access for exactly this repository (see [project code availability](project-code-availability.spec.md)), within its deadline, and is torn down | `env_repository_unreachable` |
| The leased executor has a credential and an installed CLI in a preflight session (3.3.1, "Executor probe"), for a task that delegates to a probed executor | `env_executor_auth` |
| The task workspace has a place: the agent group dir is known and the task has a repository URL | `env_workspace_unconfigured` |
| The task workspace can be created and read (no `EACCES`, `EPERM` or `EROFS`) | `env_workspace_permission` |
| The task workspace is a real directory under the workspaces root and a git repository | `env_workspace_invalid` |
| Its `origin` is the task's repository URL, without credentials (a GitHub owner and repository may differ in letter case only) | `env_workspace_remote_mismatch` |
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
| Every selected Kubernetes integration is active, unexpired, validated and its credential is in the store ([integrations](integrations.spec.md) 3.6) | `env_integration_unavailable` |
| The worker's engine and any delegated executor are Claude | `env_integration_unsupported` |
| The attempt's kubeconfig directory is private, not a link, and the credential matches the selection | `env_integration_invalid` |
| The worker's Kubernetes route listens where sessions reach it, and opens a tunnel to each selected cluster's exact `host:port` at a non-internal address (integrations 3.6 Route) | `env_integration_route_unavailable` |
| Through that tunnel, each cluster's TLS handshake verifies against the integration's CA and names its host: nothing in between terminates TLS (integrations 3.6 Route) | `env_integration_gateway_intercepts` |
| A guarded engine's managed settings register the command guard hook, when the guard is enabled ([command guard](command-guard.spec.md) 3) | `env_guard_missing` |

- The executor probe runs after the repository checks and before the inputs and the
  workspace, for every task that delegates to a probed executor.
- The workspace checks run for every task whose project has a configured
  repository, after the repository checks; [task workspace](task-workspace.spec.md)
  3.4 defines them.
- The input checks run for every task with a required input, before the
  workspace is prepared and before any model runs
  ([task inputs](task-inputs.spec.md) 3.4).
- The integration checks run for every task that selected an integration, after
  the inputs and before any model runs ([integrations](integrations.spec.md) 3.6).

### 3.3.1 Repository preflight session

The NanoClaw host has no OneCLI HTTPS proxy: only a session container is configured
with the proxy and its CA, so a probe run in the host process can never reach the
forge (it fails with `OneCLI HTTPS proxy is not configured`). The repository check
therefore runs in a bounded, ephemeral, model-free session:

- **Lease and assignment check first.** Before any session or forge request, the host
  confirms, with OneCLI admin reads only (`GET /agents`, `GET /secrets`,
  `GET /agents/{id}/secrets`): a task-scoped `repository` lease is held for this task
  with the repository's provider; the OneCLI agent of the agent group exists; both
  repository secrets (`argus-<group>-repository-rest` and `-git`) exist and are assigned
  to that agent; and, when OneCLI lists them, their host and path patterns are exactly
  the repository's REST and Git scopes. When the lease carries a GitHub alias spelling
  ([GitHub repositories](github-repositories.spec.md) 3.3), both alias secrets are
  checked the same way; an alias assigned when none is expected is a scope mismatch. The preflight session must then run for that same
  agent group. A failed check is outcome `assignment_missing` (a check that cannot be
  read, `assignment_unverified`) and blocks with `env_repository_unavailable`, not as a
  forge denial. The check writes nothing, so a concurrent worker session's lease and
  every assignment stay as they are.
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
- **Four layers, kept apart.** The preflight distinguishes (1) the **registered lease** the
  host holds (`lease`), (2) the **desired OneCLI assignment** (`assignment`, admin reads),
  (3) the **effective published policy** the gateway injects by, and (4) the **observed
  injection** on the probe request. Layers 3 and 4 are not readable through OneCLI's admin
  API (only `GET /agents`, `/secrets` and `/agents/{id}/secrets` exist in the adapter), so
  the event reports them as `injection` (`observed` when the answer proves authentication,
  `absent` when a 200 has no permissions object or independent evidence says so, else
  `unknown`) and `independent_evidence` (`not_consulted`, `unavailable`, `missing`,
  `observed`, `inconclusive`, `mismatch`, `error`), and a closed `diagnosis` (`none`,
  `assignment`, `missing_injection`, `injection_unverifiable`, `injection_evidence_mismatch`,
  `forge_denied`, `unknown_evidence`, `network`, `session`, `rearm_failed`,
  `rearm_exhausted`, `rearm_unproven`). A matching assignment never proves injection.
- **Bounded re-arms when no credential was attached.** The gateway injects only through the
  policy rule OneCLI publishes for the assignment, and that publication can lag the admin read
  or fail. The host re-applies the repository lease (the same secrets and assignments, the
  same host- and path-exact scopes; no new lease; idempotent PATCH/PUT) and probes again, up
  to 4 rounds, strictly serial, waiting 0.5 s, 1 s, 2 s and 4 s before each with equal jitter
  (a random wait between half the step and the step, never above 4 s; the clock, the random
  source and the sleep are injectable). The whole window has a 20 s wall-clock budget
  (`REARM_WINDOW_MS`): a round whose wait would pass it is not started and the preflight ends
  `rearm_exhausted`. Preflights for the same OneCLI agent and repository share one in-flight
  re-arm and probe round (the later caller awaits the first one's answer or failure instead of
  re-applying the lease again); the entry is dropped when the round settles. Only two
  shapes authorize a re-arm, and only with the lease held and the assignment confirmed for
  this exact repository:
  1. a probe that fails with the forge's 200 and no permissions object (the helper's
     `policy_denied`), which shows no credential was attached;
  2. a **404 that proves no authentication** (the probe keeps no body, so a missing repository
     and a private one nothing was injected for look alike) **together with independent
     exact-agent evidence** that the injection is missing. The runtime supplies that evidence
     through `InjectionEvidenceSource`; its answer must name this OneCLI agent and this
     repository (`origin/owner/repo`), else it is ignored (`mismatch`). **Production supplies
     none** (`unavailableInjectionEvidence`): no supported OneCLI policy-rule or request-log
     read exists, so such a 404 ends `unknown` with diagnosis `injection_unverifiable`, which
     names the missing evidence source. A 404 is never classified as transient and a retry
     alone is never a fix.

  Everything else is final: a 401/403, an answer that proves a credential (including a
  missing push or read permission), a bare 404, a network error, a session failure, another
  agent's or repository's evidence, another worker's success, a stale or revoked lease. A
  re-arm that throws stops with `rearm_failed` and its non-secret hint (the probe's evidence is
  kept in the event); a failure that survives all rounds stops with `rearm_exhausted`. The cap,
  the backoff and the counters are unchanged, nothing is requeued, and no attempt or failure
  count is reset. After a re-arm only a probe that proves an authenticated credential and the
  required access ends the preflight `ok` (`rearm_unproven` otherwise). The write-access denial
  hint depends on the verdict: when it shows no credential was attached (200, no permissions
  object, authentication unknown) it says the gateway attached none because its policy rule is
  not materialized in OneCLI and to re-apply the repository assignment, rather than that the
  forge credential lacks push access, which is kept for a verdict that carried a credential.
- **Outcomes.** Every preflight ends with one outcome from a closed set, named first in the
  block `error` (`repository preflight <outcome>: …`):

  | Outcome | When | Block reason |
  | --- | --- | --- |
  | `ok` | Accepted (below) | none |
  | `assignment_missing` | No task-scoped lease, no OneCLI agent, a repository secret absent, unassigned or scoped to another host or path, the session's agent group is not the lease's agent, or the re-applied lease for GitHub's verified spelling is not confirmed | `env_repository_unavailable` |
  | `assignment_unverified` | OneCLI's admin API could not be read for the check, or re-applying the lease for GitHub's verified spelling failed | `env_repository_unavailable` |
  | `forge_denied` | The forge answered 401 or 403, or a 404 or other response that proves a credential was attached (a private repository) lacks the required permission. A 401 does not say whether the credential was absent or invalid | `env_repository_unreachable` |
  | `network_failure` | The request did not get a forge answer: proxy or network error, timeout | `env_repository_unreachable` |
  | `unknown` | The answer cannot prove authentication or token scope: a public (or unlabelled) repository whose response lacks the permissions object or the required permission, a 404 that proves no authentication (a missing repository, or a private one nothing was injected for), or any other forge answer (5xx, rate limit, another repository) | `env_repository_unreachable` |
  | `session_failure` | The preflight session itself failed: no proxy in the contribution, a driver refusal, a missing verdict, the deadline, an unavailable gateway, an incomplete teardown | `env_repository_unreachable` |

  A response that does not prove a credential is never called anonymous: an unproven case is
  `unknown`, and the evidence says what was missing.
- **Evidence events.** Each preflight emits one evidence event, correlated by task, attempt
  and a random preflight id: provider, access, the lease check (held, role, provider), the
  assignment checks (agent, each secret's state, scope match, secret mode), the target API
  and Git hosts and their scoped path patterns, the probe outcome with the forge's HTTP
  status, whether the permissions object was present, the push bit, the repository's
  visibility and whether authentication is proven, whether the lease was re-armed and how
  many times (`rearm_attempts`) and over how many wall-clock ms (`rearm_window_ms`, 0 when not
  re-armed), the `fault` (`gateway_policy_publish` when a re-arm ended `rearm_exhausted` or
  `rearm_unproven` with no credential attached, else `none`: a OneCLI policy-publish fault, not
  a permission or network problem; a final `rearm_exhausted` error also says so, with the
  attempt count and window from the event), the `canonical` spelling decision
  ([GitHub repositories](github-repositories.spec.md) 3.3), the `injection`, `independent_evidence` and `diagnosis` values above, and the evidence source of each part
  (`onecli_admin_read`, `preflight_session_verdict`, `host`). Every field is a closed value,
  a bounded integer, or a host and path the host derived itself; nothing the container wrote
  is copied as text. No secret value, header, proxy URL or userinfo, or credential-bearing URL
  is in an event. The channel logs it and posts it as one line to the task's live log for the
  attempt (best effort; a failed post never changes the outcome).
- **What the task is told.** Nothing the container wrote is passed on as text. A failing child
  is reported as a closed reason: its outcome and the helper's error code if they are in their
  fixed sets, else "of an unknown kind", else "no verdict was written". For `policy_denied` the
  helper also names the policy rule (`repository_write_access`, `repository_read_access`,
  `repository_allowlist`, `forge_proxy`); the host checks it against its own closed table and
  appends the rule, the host-derived repository (`origin/owner/repo`) and a fixed remediation
  hint, or "rule not reported by the helper". The block reason stays the closed code; the
  specifics travel in the block `error`. Provider and repository fields in the verdict are
  compared, never echoed. Errors from the driver and OneCLI are
  printable-only, credential-scrubbed and cut to 160 characters, and the channel redacts and
  bounds the block `error` again.
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
  repository with `allowlist_refuses_other_host` and `allowlist_refuses_sibling` true and,
  for write, `authenticated` and `write` true; for read, `read` true. `authenticated` is true
  only when the response proves a credential was attached (`push` true, or a private
  repository answered); a public repository's `pull` bit does not, so a read verdict may say
  `authenticated: false` with `authentication: unknown` in its evidence. The driver reports
  a non-zero exit as `failed` with the code and exit 0 as `stopped`; because Docker's transient `removing` state also reads
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
| OneCLI assignment check (admin reads) | The lease's agent has both repository secrets assigned, scoped (when listed) to the repository's REST and Git host and path. | That the proxy injected them on the probe request, or that the stored value is a valid token. |
| `permissions.push` true for the same `owner/repo`, read by the helper through the session's proxy | The credential the gateway attached to that request is authenticated and may write to that repository, as the forge reports it. An anonymous read of a public repository cannot produce it. | That OneCLI's host/path rules stop the token reaching other hosts or repositories; that the token has no broader rights or its OAuth or fine-grained scope; Git smart-HTTP access (a different secret). |
| A public repository's response without `permissions` or without the required bit | Nothing about authentication: outcome `unknown`. | That no credential was attached (it may be attached and lack rights), or that injection failed. |
| `allowlist_refuses_other_host`, `allowlist_refuses_sibling` | The helper's own allowlist policy code refuses another host and a sibling repository (a local check, no network). | Anything about the proxy, OneCLI scoping or the token. Treat as a self-test of the helper in the deployed image. |
| Offline tests with a fake docker CLI | The host's control flow: deadlines, late cleanup, teardown failures, verdict handling, what is accepted. | Forge or gateway behaviour. |
| Container tests (`argus-repo-preflight.docker.test.ts`, set `ARGUS_PREFLIGHT_DOCKER_IMAGE`) against a synthetic CONNECT proxy and forge with a throwaway CA, and a synthetic OneCLI admin API | With a real daemon and driver: the container user writes the verdict, exit codes (13, 15, 137) and removal, the deadline, a planted symlink is not followed, a wrong user fails closed; the outcomes for an injected, an absent (no assignment: no container), a denied and an unproven public answer; no proxy userinfo or token in events, errors or logs. The synthetic forge's answers are NOT real GitHub authorization. | Any real forge, real OneCLI, a real token or the production image. |
| The rollout canary ([worker lifecycle](worker-lifecycle.spec.md)) | The real OneCLI account and repository. This is the only evidence of production behaviour. | |

- `env_workspace_missing` is not a preflight check: the channel blocks with it
  when the agent group directory stays missing on the host for 90 s while the
  task is claimed ([stall visibility](stall-visibility.spec.md) 3.7).
- `env_executor_auth` is the executor probe's reason (below). A worker whose
  executor passes the probe but is rejected by its provider later reports it with
  `FAIL (environment):` (3.4).
- `BlockRequest` gains an optional `error` (at most 1,500 characters, redacted by
  the channel like the live log), stored as the task's `error`. A worker may block
  only with an `env_*` reason from `ENV_BLOCK_REASONS` or the reasons it sends
  today; anything else is `422`.
- Steps after the preflight fail with their own reason and the real error:
  page inlining and injection as `channel_error` naming the step. The blanket
  "repository credential unavailable" message goes away.
- A blocked `env_*` task shows in the inbox under its project, as blocked tasks
  already do ([MCP operations](mcp-operations.spec.md) 3.2).

**Executor probe.** The same session machinery runs `argus-delegate EXECUTOR
--auth-probe` as PID 1 for the leased (or pinned) executor, before any model session,
with the same deadline, teardown and gateway lease. It runs the delegate's own pre-run
gates (credential present for that executor, CLI installed: `--version`) and nothing else.
Exit 0 proceeds; 78 (no credentials) and 127 (not installed) block `env_executor_auth`
with fixed host-written text (at most 1,500 characters, scrubbed): nothing the container
wrote is copied. No attempt is spent and no model runs. It probes `claude`, `codex` and
`agent` (Cursor), whose credential the session sees from the gateway alone; `opencode`,
`copilot` and custom or local accounts depend on policy context or env the worker path
adds, so they are not probed. A task with no delegated executor is not probed.

The probe keeps four states apart, and only the first two can block:

1. **Credential present** in the session (exit 78 when absent). Presence only: the
   delegate never reads a credential's content, so a revoked, expired or exhausted one
   passes.
2. **CLI available** (`--version` answers; exit 127 when the binary is missing).
3. **Provider acceptance**: not proven by the probe, by any report field or by any
   code path. Nothing is called "authenticated" on its evidence.
4. **Unknown / inconclusive**: a session that cannot finish, a failed teardown, a probe
   that throws, an image whose delegate lacks `--auth-probe` (exit 64), an unknown exit
   code, or a CLI whose `--version` does not answer in 15 s (exit 124,
   `inconclusive: "version_timeout"`). The task proceeds under the delegate's own gate
   at run time; an inconclusive probe is never `ok` and never a block.

| Evidence | Proves | Does not prove |
| --- | --- | --- |
| `argus-delegate --auth-probe` exit 0 | In a session of the agent image with the gateway contribution, the delegate's credential gate passes for that executor and its CLI answers `--version`. | That the provider accepts the credential (a revoked, expired or exhausted one passes), that the gateway injects it on the provider's host, the account's quota, or any model. The CLI is never asked to authenticate or run. |
| Exit 124, 64, other, or a probe error | Nothing. | Whether the credential or the CLI is usable; the task proceeds. |
| Exit 78 / 127 | No credential was visible to the executor in the session, or its CLI is missing. | Which side is at fault (lease not applied, stub not mounted, image); the text names both. |
| Offline tests with a fake docker CLI | The host's control flow: argv, exit-code mapping, redaction, teardown on timeout. | Any real executor, gateway or image. |

## 3.4 The worker can say "environment"

The reply protocol gains `FAIL (environment): <why>` (in the scoped form,
`FAIL on <task-id>: (environment) <why>`). The channel blocks the task
as `worker_env` with `<why>` as `error`, instead of failing it. The task prompt
says to use it only when the workspace, tools or credentials are missing, not
when the work is hard. Any other `FAIL:` is `worker_fail`.

## 3.5 Text-classifier evidence rule

**A failure class is attributed from evidence the work cannot write: a
structured field, an exit status, or a runner line anchored at the start of
the error.  A wrapper keeps the class it receives and never replaces it with
its own nearest generic category.**

Each classifier that turns engine or process output into a failure class,
provider failure, or stall cause is listed here with the evidence it accepts
and whether the model's or the work's own output can forge it.

| Classifier | Source | Anchoring | Exit code | Forgeable? |
| --- | --- | --- | --- | --- |
| `quota.py classify_quota()` — quota / auth | Provider-level error text; structured JSON `codexErrorInfo`/`code`/`type`; `auth_expired` notices | `_PREFIX` stripped then patterns anchored at `^` via `_fold` → `_PREFIX.sub` + `body` start | No | No — anchored at start after runner prefix |
| `quota.py failure_class()` — network | Runner's own error head after `_PREFIX`, `_CHANNEL_STEP`, `_JS_ERROR` stripped | `NETWORK_NOTICES.match(head)` — `^` anchored | No | No — only the runner's first line classifies |
| `overlays/claude-rate-limit.ts classifyRateLimit()` | SDK `RateLimitError` mark or one exact whole-line notice | Pattern anchored at `^` after prefix strip | No | No — SDK mark or exact runner line |
| `overlays/codex-usage-limit.ts codexUsageLimitFailure()` | Structured `codexErrorInfo`/`code`/`type` field, or runner-prefixed text | `LIMIT_TEXT` anchored at `^` after `NOTICE_PREFIX` strip; structured path bypasses text match | No | No — anchored after prefix strip |
| `argus.ts` stall causes (`argus-stall-causes.test.ts`) | Channel's own stall detection logic | Channel-side, not from model output | No | No — channel-internal |
| `argus-oom` OOM classification | Container runtime `docker inspect` structured data: `OOMKilled`, `ExitCode 137`, `Name` | Structured field (3.2.1) | Yes (137 required) | No — only container runtime data |

# 4. Open Questions

## Open items

- Full recovery of a private-repository 404 whose injection is missing is pending a
  supported OneCLI evidence or rule read (published policy rule or request log for the exact
  agent). The host-side interface (`InjectionEvidenceSource`) and the bounded re-arm exist and
  are tested, but production wires no source, so that 404 stays fail-closed
  (`injection_unverifiable`). The spec moves to `implemented` when such a source is wired.

## Questions

- Should one `env_repository_*` block hold the project's other ready tasks until
  it is unblocked, or is one cheap preflight per task fine?
- Is blocking on the first `worker_fail` too strict for tasks that would pass on a
  plain retry? Re-check after two weeks against the baseline.
- Is 2 consecutive OOM-killed sessions the right threshold for `env_oom`? It is
  owner policy, kept as a constant (`OOM_PROMOTE_STREAK`). The scope is decided:
  per pool, consecutive outcomes, no time window (3.2.1).

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
  `0058_events_task_kind.sql` adds the partial index it seeks; the new event
  `task.auto_resumed`. The channel's `reportExit` sends the fail once per
  heartbeat: a transient error keeps the task, so the heartbeat renews its lease
  (observed `gone`) and the next one repeats the same report, with one report in
  flight at a time; a `409` or a rejection ends it, as does Argus ending the
  claim. It logs the state Argus answered.
- OOM (3.2.1): migration `0061_oom_streak.sql` adds `tasks.oom_kill` and
  `pools.oom_streak`; `FailRequest.oom_kill`; `env_oom` (`ENV_OOM`) in
  `FAILURE_CLASS` as `environment` and in Fleet `REASONS`, but not in
  `ENV_BLOCK_REASONS`: a worker cannot send it as a block; `PoolHealth.oom_streak`; `Task.oom_kill`; the channel's
  `readSessionEvidence` reads `OOMKilled` and `Name`, and `reportExit` sends the
  fact with each repeat of the same report. The operator smoke is the
  [OOM smoke](../runbooks/2026-10-31.oom-smoke.ref.md) runbook.

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
  - `agent_ended` follows the rule only after `gone` observations held for 90 s
    after the claim, up to the last one; one `gone`, a shorter run, a run broken
    by `running` or by a renewal without an observation, and a run dated before
    the claim do not;
  - concurrent and repeated fails record one `task.auto_resumed`, and a stale or
    lost holder is refused;
  - the error is capped in UTF-8 bytes without splitting a 2-, 3- or 4-byte
    character, and the event carries no error text;
  - `EXPLAIN QUERY PLAN` shows each read is one seek on `events_task_kind`.
  - The channel tests (fake runtime, scripted Argus) cover one report with the
    attempt and a redacted error, a transient error repeated on each heartbeat
    with the lease renewed and the evidence read once, one report in flight when
    heartbeats overlap, no repeat after a `409` or after Argus ends the claim, and
    a plain reply posted as progress before the exit.
  - OOM (3.2.1, `apps/api/tests/test_oom_promotion.py`, the channel's
    `argus-oom.test.ts` with a fake `docker inspect`): the fact only for
    `OOMKilled` true, exit 137 and the seen container; none for exit 137 alone,
    a kill, a stop, another code, a failed or slow inspect, a foreign container
    or log text; the first OOM keeps 3.2 with and without progress; the second
    on one pool blocks `env_oom` with the accounting above; a non-OOM outcome,
    another pool, a stale fail and the reaper do not promote; unblock and a
    memory change reset, a retry does not; the cap still ends the loop.
- Executor probe (3.3.1): `runExecutorPreflight` in `argus-repo-preflight.test.ts`
  (exit 0, 78 and 127 mapping, redaction, an unprobed executor, and a hung session or
  exit 64, 124, 1 or none as inconclusive); `argus-delegate.test.mjs` (`--auth-probe`
  exit 0/78/127 with nothing run, and a `--version` that hangs is 124, never ok or 127);
  the channel tests in `argus-accounts.test.ts` ("executor credential probe in the
  channel": a 78 or 127 block is `env_executor_auth` with the attempt, no session, no
  `/fail`; no delegated executor is never probed; a probe that throws proceeds and
  reports nothing authenticated; a clean probe runs before the session). Not verified:
  provider acceptance of any credential, and a real image's delegate.
- Not verified: real Docker sessions dying, a real NanoClaw host lost mid-claim,
  whether Docker sets `OOMKilled` when a non-init process of the session is
  killed (the [OOM smoke](../runbooks/2026-10-31.oom-smoke.ref.md) checks the
  init case on a disposable container), and any deployed node.
- Accepted as is: the server trusts a `session_lost` from the lease holder (the
  channel's 90 s check is the confirmation; a fenced worker can only spend its
  own attempt), and the rule's reads are separate `LIMIT 1` seeks rather than
  one combined query.
