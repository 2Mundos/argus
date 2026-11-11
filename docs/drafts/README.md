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
| [Worker efficiency: stop paying for work that is thrown away](2026-10-24.worker-efficiency.draft.md) | `partial` |
| [Task operations and reliability](2026-10-29.task-operations.draft.md) | `partial` |
| [Verification boundary in specs](2026-10-29.verification-boundary.draft.md) | `implemented` |
| [Replay engine](2026-10-31.replay-engine.draft.md) | `implemented` |
| [DeepSeek Harness as an engine](2026-11-04.dsh-engine.draft.md) | `partial` |
| [Engine transcript replay](2026-11-04.engine-transcript-replay.draft.md) | `partial` |
| [Failure evidence audit](2026-11-04.failure-evidence-audit.draft.md) | `implemented` |
| [Postmortems](2026-11-04.postmortems.draft.md) | `implemented` |
| [Repeat-call reminder](2026-11-04.repeat-call-reminder.draft.md) | `partial` |
| [Tool description rules](2026-11-04.tool-description-rules.draft.md) | `draft` |
| [Project review recommendations](2026-11-06.project-review-recommendations.draft.md) | `draft` |
| [Premises review: yield before features](2026-11-11.premises-review.draft.md) | `draft` |
