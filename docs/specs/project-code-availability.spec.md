# Project code availability

# 1. Purpose and Scope

Orchestrators should not spend turns wiring every implementation task with
`repository:write` just so a worker can read the project's tree. Today, checkout
only runs when the task lists that capability; tasks created with empty
capabilities arrive in a container with an empty `/workspace/group` and no git
tree, even when the project's repository is configured on the pool.

**Project code availability** is automatic: any task whose **project** has a
repository URL in the pool's `nanoclaw.repositories` map gets that repository
prepared before the model runs, without the orchestrator naming a capability.
Push, forge mutation, checkpoints, and deliverable rules that need write evidence
stay gated on `repository:write` and on branch/pr deliverables ([task
deliverable](task-deliverable.spec.md)).

In scope: when the channel takes a lease, which preflight runs, workspace
preparation, prompt lines, and how this relates to existing repository and
workspace specs.

# 2. Non-Goals

- No orchestrator-set **`repository:read`** capability; availability is implied
  by project + pool configuration, not a field on `TaskCreate`.
- No change to **deliverable** creation rules: branch/pr_* still require
  `repository:write` at create time; Argus still does not add that capability for
  the caller.
- No host-side clone or mount of a canonical checkout ([task
  workspace](task-workspace.spec.md) non-goals stand).
- No read-only **forge API** lease separate from today's `repository` account
  lease: the same task-scoped lease and OneCLI path apply; preflight distinguishes
  read vs write.
- Tasks on **`global`** never get automatic read access, even if the pool map
  lists a repository for `global` (that project is for knowledge and settings;
  the API refuses task creation on `global`). Projects with no configured
  repository are unchanged (no tree, no lease).

# 3. Decisions Made

## 3.1 When code is provided

| Condition | Channel behaviour |
| --- | --- |
| Project has no entry in the pool repositories map | Unchanged: no repository lease, no workspace, no `task.repository_url`. |
| Project has a configured repository | Before injection: resolve URL, take the task-scoped **`repository` lease**, apply OneCLI secrets, confirm both are assigned to the agent (OneCLI admin reads), run repository preflight ([failure classes 3.3.1](failure-classes.spec.md)), prepare the [task workspace](task-workspace.spec.md), set `task.repository_url`, include workspace and read rules in the trusted prompt. |
| Same, and task lists **`repository:write`** | Preflight requires **write** (`permissions.push` on the forge GET, as today). Worker may fetch, commit, push, and use forge helpers for mutation. Checkpoints and publish paths unchanged. |
| Same, and task does **not** list **`repository:write`** | Preflight requires **read** (repository GET with pull/admin/push on the forge permissions object; on a public repository this does not prove the credential was attached, and the evidence says `authentication: unknown`). Workspace is the same path and branch naming; prompt states **read-only**: no push, no publishing commits, no opening PRs. `argus-delegate` and forge helpers that mutate the remote remain unavailable without write. |

Every attempt of the task on the same instance reuses the same workspace key;
validation rules in task workspace 3.4 apply to all prepared workspaces.

## 3.2 Preflight

The ephemeral session ([failure classes](failure-classes.spec.md) 3.3.1) runs
`repo-preflight` with `--access write` (default, backward compatible) or
`--access read`. The verdict records `authenticated`, `write`, and `read`. Write
mode matches the existing gate. Read mode fails closed when the credential cannot
read the configured repository.

## 3.3 Orchestrator and API surface

- **`create_task` / drafts / edits:** unchanged required fields for code work.
  Orchestrators set **`deliverable`** for what counts as done and
  **`repository:write`** only when the task may mutate the remote or must satisfy
  branch/pr deliverables.
- **Skill and MCP** describe automatic read access for configured projects so
  orchestrators do not duplicate checkout instructions or omit write when they
  intend PRs.

## 3.4 Checkpoints and cleanup

Checkpoint restore before workspace prepare runs only for tasks with
`repository:write` ([task checkpoint](task-checkpoint.spec.md)). Read-only tasks
may still hold a workspace directory from a prior attempt; they do not get
checkpoint resume.

# 4. Open Questions

- Should Argus **default** `deliverable` to `pr_open` for certain kinds when
  `repository:write` is present (deliverable spec §4)?
- Public repositories without a token: allow anonymous read preflight or always
  require the lease (current decision: always lease when the project is
  configured, same as write tasks).
- Model-free `git fetch` of the default branch into the workspace before the
  first model message (so read tasks never depend on the model to populate the
  tree).
- OneCLI path restrictions for read tasks (block `git-receive-pack`, mutating
  REST) when preflight used `--access read`.

# 5. Constraints

- Single node; no new tables; no network inside host workspace prepare.
- Forge reads for accept gates stay outside write transactions.
- English only.

# 6. Implementation Consequences

- `integrations/nanoclaw/gitea-api.mjs` (and GitHub helper): `repo-preflight
  --access read|write`.
- `integrations/nanoclaw/argus-repo-preflight.ts`: pass `--access`, assert read or
  write verdicts.
- `integrations/nanoclaw/argus.ts`: repository preflight and workspace prepare for
  all project-repository tasks; read-only prompt lines.
- [Task workspace](task-workspace.spec.md) 3.2, [worker
  lifecycle](worker-lifecycle.spec.md) repository section, [failure
  classes](failure-classes.spec.md) 3.3 table: rewritten to match.
- `skills/argus-orchestrator/SKILL.md`, MCP `create_task` / server instructions.
- Tests: helper read/write preflight, channel preflight wiring, prompt lines.

## Verification boundary

- Verified by unit and channel tests with fake forge responses.
- Not verified: every production pool configuration and live forge.
- **Read enforcement** is preflight mode, prompt, completion/deliverable gates, and
  worker discipline today; the same OneCLI git and REST paths remain available
  when the account token can write. A gateway read-only scope is an open hardening
  step, not part of this change.
- **Tree contents** on a first attempt: the host prepares an empty workspace with
  `origin` set; the worker session runs `git fetch` (trusted prompt) before work,
  as for existing `repository:write` tasks. A model-free fetch before injection is
  deferred (open question below).
