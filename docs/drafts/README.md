# Drafts index

Every draft in this directory with its status, kept in sync by
`scripts/check_specs.py` (CI and pre-push fail otherwise; regenerate with
`python3 scripts/check_specs.py --write-index`). Status meanings:

- `implemented`: main does what it says, built without a spec of its own.
- `partial`: some of it is not built yet; its **Open items** section lists what.
- `draft`: proposed or agreed, not built.
- `deferred`: parked; not planned for now.
- `superseded`: replaced by the spec it links.

| Draft | Status |
| --- | --- |
| [Knowledge — Draft](2026-10-14.knowledge.draft.md) | `superseded` |
| [Worker Lifecycle and Task Coordination — Draft](2026-10-14.worker-lifecycle.draft.md) | `superseded` |
| [Worker efficiency: stop paying for work that is thrown away](2026-10-24.worker-efficiency.draft.md) | `partial` |
| [Agent container resource hygiene](2026-10-29.agent-resource-hygiene.draft.md) | `superseded` |
| [Account auth health: presence is not validity](2026-10-29.auth-health.draft.md) | `superseded` |
| [Bounded startup and recovery](2026-10-29.bounded-recovery.draft.md) | `superseded` |
| [Command guard for agent shells](2026-10-29.command-guard.draft.md) | `superseded` |
| [Disk guard: warn, pause, then shed](2026-10-29.disk-guard.draft.md) | `superseded` |
| [Harness stderr tail on failed attempts](2026-10-29.harness-stderr-tail.draft.md) | `superseded` |
| [Resume the same conversation after a quota pause](2026-10-29.quota-resume.draft.md) | `superseded` |
| [Task operations and reliability](2026-10-29.task-operations.draft.md) | `partial` |
| [Verification boundary in specs](2026-10-29.verification-boundary.draft.md) | `implemented` |
| [Changed-only checks](2026-10-31.changed-only-checks.draft.md) | `implemented` |
| [Evals for model-facing text](2026-10-31.model-facing-evals.draft.md) | `superseded` |
| [Replay engine](2026-10-31.replay-engine.draft.md) | `implemented` |
| [Test hygiene checks](2026-10-31.test-hygiene-lint.draft.md) | `implemented` |
| [Worktree dev isolation](2026-10-31.worktree-dev-isolation.draft.md) | `implemented` |
| [DeepSeek Harness as an engine](2026-11-04.dsh-engine.draft.md) | `draft` |
| [Engine transcript replay](2026-11-04.engine-transcript-replay.draft.md) | `draft` |
| [Failure evidence audit](2026-11-04.failure-evidence-audit.draft.md) | `implemented` |
| [Postmortems](2026-11-04.postmortems.draft.md) | `implemented` |
| [Repeat-call reminder](2026-11-04.repeat-call-reminder.draft.md) | `draft` |
| [Tool description rules](2026-11-04.tool-description-rules.draft.md) | `draft` |
