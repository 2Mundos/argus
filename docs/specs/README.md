# Specs index

Every spec in this directory with its status, kept in sync by
`scripts/check_specs.py` (CI and pre-push fail otherwise; regenerate with
`python3 scripts/check_specs.py --write-index`). Status meanings:

- `implemented`: main does what it says.
- `partial`: some of it is not built yet; its **Open items** section lists what.
- `draft`: proposed or agreed, not built.
- `deferred`: parked; not planned for now.
- `superseded`: replaced by the spec it links.

| Spec | Status |
| --- | --- |
| [Global account selection](account-selection.spec.md) | `implemented` |
| [Account usage](account-usage.spec.md) | `implemented` |
| [Agent hygiene: lost/gone aggregates, bounded archiving and alerts](agent-hygiene.spec.md) | `implemented` |
| [Agent container resource hygiene: OOM priority and CPU count](agent-resource-hygiene.spec.md) | `implemented` |
| [Built-in Orchestrator Runtime](builtin-orchestrator.spec.md) | `implemented` |
| [Changed-only checks](changed-only-checks.spec.md) | `implemented` |
| [Claude accounts: import, dedicated login, renewal](claude-accounts.spec.md) | `implemented` |
| [Codex accounts: import, dedicated login, engine selection](codex-accounts.spec.md) | `implemented` |
| [Codex worker sandbox profile (default on; profiles candidate)](codex-sandbox-profile.spec.md) | `partial` |
| [Command guard](command-guard.spec.md) | `partial` |
| [Copilot accounts: import, executor lease, delegated CLI](copilot-accounts.spec.md) | `implemented` |
| [Cost view](cost-view.spec.md) | `implemented` |
| [Cursor accounts: import, executor lease, delegated CLI](cursor-accounts.spec.md) | `implemented` |
| [Custom OpenAI-compatible accounts](custom-accounts.spec.md) | `implemented` |
| [Custom OpenAI-compatible harness (NanoClaw engine)](custom-harness.spec.md) | `implemented` |
| [Dashboard action feedback](dashboard-actions.spec.md) | `implemented` |
| [Dashboard navigation](dashboard-navigation.spec.md) | `implemented` |
| [Dashboard live data performance](dashboard-performance.spec.md) | `implemented` |
| [Dashboard working indicator](dashboard-working.spec.md) | `implemented` |
| [Engines, executors and the local model](engines.spec.md) | `implemented` |
| [Failure classes](failure-classes.spec.md) | `implemented` |
| [Fleet health](fleet-health.spec.md) | `implemented` |
| [Gitea helper for workers (`argus-gitea-api`)](gitea-helper.spec.md) | `implemented` |
| [GitHub repositories](github-repositories.spec.md) | `implemented` |
| [Home](home.spec.md) | `implemented` |
| [Internal Orchestrator](internal-orchestrator.spec.md) | `implemented` |
| [Knowledge Spec](knowledge.spec.md) | `implemented` |
| [Launcher](launcher.spec.md) | `implemented` |
| [Live work](live-work.spec.md) | `implemented` |
| [Local Strata harness (NanoClaw failover)](local-harness.spec.md) | `implemented` |
| [Local workers](local-workers.spec.md) | `implemented` |
| [Login clients](login-clients.spec.md) | `implemented` |
| [MCP operations](mcp-operations.spec.md) | `implemented` |
| [Model-Facing Evals](model-facing-evals.spec.md) | `implemented` |
| [Model observability](model-observability.spec.md) | `implemented` |
| [Preferred models](model-selection.spec.md) | `implemented` |
| [Onboarding](onboarding.spec.md) | `implemented` |
| [OneCLI cleanup: vault entries of terminated NanoClaw instances](onecli-cleanup.spec.md) | `implemented` |
| [Operations Spec](operations.spec.md) | `implemented` |
| [Pool delete](pool-delete.spec.md) | `implemented` |
| [Pool editor](pool-editor.spec.md) | `implemented` |
| [Progress milestones](progress-milestones.spec.md) | `implemented` |
| [Project code availability](project-code-availability.spec.md) | `implemented` |
| [Project runtime images](project-runtime.spec.md) | `implemented` |
| [Prompt-data isolation (task spec, knowledge, child tasks)](prompt-data-isolation.spec.md) | `implemented` |
| [Quota resume: resume the same native conversation after a quota pause](quota-resume.spec.md) | `implemented` |
| [Releases](releases.spec.md) | `implemented` |
| [Routines](routines.spec.md) | `implemented` |
| [Security hardening (2026-10-20 review, medium/low)](security-hardening.spec.md) | `implemented` |
| [Settings](settings.spec.md) | `implemented` |
| [Single-layer experiment: the Claude harness codes without the delegate hop](single-layer-experiment.spec.md) | `implemented` |
| [Lightweight Single-Node Spec](single-node.spec.md) | `implemented` |
| [Stall visibility](stall-visibility.spec.md) | `implemented` |
| [Task archive](task-archive.spec.md) | `implemented` |
| [Task changes](task-changes.spec.md) | `implemented` |
| [Task checkpoint: keep a task's unpublished work across attempts and workers](task-checkpoint.spec.md) | `implemented` |
| [Task continuation](task-continuation.spec.md) | `implemented` |
| [Task cost and budget](task-cost.spec.md) | `draft` |
| [Task deliverable](task-deliverable.spec.md) | `implemented` |
| [Task delivery](task-delivery.spec.md) | `implemented` |
| [Task dependencies](task-dependencies.spec.md) | `implemented` |
| [Task difficulty, executor routing and the hard tier](task-difficulty.spec.md) | `implemented` |
| [Task discovery and prerequisite evidence](task-discovery.spec.md) | `implemented` |
| [Task drafts](task-drafts.spec.md) | `implemented` |
| [Task edit](task-edit.spec.md) | `implemented` |
| [Task finalization](task-finalization.spec.md) | `implemented` |
| [Task helpers (`argus-task`)](task-helpers.spec.md) | `implemented` |
| [Task inputs: hand bytes to a later attempt through the NanoClaw channel](task-inputs.spec.md) | `implemented` |
| [Task live log](task-live-log.spec.md) | `implemented` |
| [Task models](task-models.spec.md) | `implemented` |
| [Task resumability preflight](task-resumability-preflight.spec.md) | `implemented` |
| [Task steering](task-steering.spec.md) | `implemented` |
| [Task workspace: a stable per-task git workspace, checked before the model](task-workspace.spec.md) | `implemented` |
| [Test hygiene checks](test-hygiene.spec.md) | `implemented` |
| [Desktop Tray](tray.spec.md) | `implemented` |
| [Worker launch and node capabilities](worker-launch.spec.md) | `implemented` |
| [Worker Lifecycle and Task Coordination Spec](worker-lifecycle.spec.md) | `implemented` |
| [Worker Read Scope](worker-read-scope.spec.md) | `implemented` |
| [Workspace cleanup: terminated NanoClaw instance directories](workspace-cleanup.spec.md) | `implemented` |
| [Worktree dev isolation](worktree-dev-isolation.spec.md) | `implemented` |
