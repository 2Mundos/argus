"""Task operations. Every write takes an open `Tx` and records its events in it.

This module is the public face: the code lives in the modules imported below,
split along the task specs, and every name callers use is re-exported here.

See docs/specs/worker-lifecycle.spec.md 3.2 and 3.3.
"""

from argus_core.tasks import MAX_DEPENDENCIES, TaskResult

from argus_api.errors import LeaseConflictError, ValidationFailedError
from argus_api.tasks.archive import archive, archive_matching, unarchive
from argus_api.tasks.claim import claim
from argus_api.tasks.continuation_ops import (
    add_clarification,
    answered_equivalent,
    continuation_of,
    observe,
    update_continuation,
)
from argus_api.tasks.create import check_create, create_task
from argus_api.tasks.edit import (
    attempt_definitions,
    attempt_summaries,
    edit_task,
    set_dependencies,
)
from argus_api.tasks.engine import (
    attempt_model_switch,
    awaiting_quota,
    engine_capacity,
    engine_choice_for,
    executor_pin,
    executor_provider,
    manages_accounts,
    prefers_local,
    record_model_switch,
    wake_waiting,
)
from argus_api.tasks.exits import (
    ATTEMPT_PROGRESS_SQL,
    EXIT_ERROR_MAX,
    LATEST_TASK_EVENT_SQL,
)
from argus_api.tasks.expiry import (
    LATE_DETECT_AFTER_S,
    OBSERVATION_STALE_S,
    QUIET_CLAIM_S,
    expire_agent_leases,
    expire_leases,
    expire_stalled,
    expire_timeouts,
    flag_quiet,
    quiet_facts,
    quiet_facts_for,
    stall_cause,
)
from argus_api.tasks.holds import (
    await_quota_ready,
    block_switch_failed,
    fail_runtime_holds,
    hold_for_runtime,
    pause_for_quota,
    release_runtime_holds,
)
from argus_api.tasks.leases import (
    Lease,
    RejectedLease,
    RenewedLease,
    _owned,
    check_lease,
    clear_paused_disk,
    mark_paused_disk,
    renew,
    renew_paused_disk,
)
from argus_api.tasks.manage import (
    ENV_DISK_FULL,
    RELEASE_REASON_MAX,
    approve,
    cancel,
    check_reviewer,
    release,
    release_agent_tasks,
    retry,
    review,
    review_failed,
    shed_disk_full,
    unblock,
)
from argus_api.tasks.reads import _row, get_task, task_row
from argus_api.tasks.reasons import (
    RUNTIME_FAILED,
    RUNTIME_PREPARING,
    RUNTIME_SWITCH_FAILED,
)
from argus_api.tasks.transitions import (
    _apply,
    check_acceptance_met,
    moot_decisions,
    planned_state,
    review_escalation,
    unmet_acceptance,
)
from argus_api.tasks.worker import block, complete, fail, report_progress

__all__ = [
    "ATTEMPT_PROGRESS_SQL",
    "ENV_DISK_FULL",
    "EXIT_ERROR_MAX",
    "LATEST_TASK_EVENT_SQL",
    "LATE_DETECT_AFTER_S",
    "MAX_DEPENDENCIES",
    "OBSERVATION_STALE_S",
    "QUIET_CLAIM_S",
    "RELEASE_REASON_MAX",
    "RUNTIME_FAILED",
    "RUNTIME_PREPARING",
    "RUNTIME_SWITCH_FAILED",
    "Lease",
    "LeaseConflictError",
    "RejectedLease",
    "RenewedLease",
    "TaskResult",
    "ValidationFailedError",
    "_apply",
    "_owned",
    "_row",
    "add_clarification",
    "answered_equivalent",
    "approve",
    "archive",
    "archive_matching",
    "attempt_definitions",
    "attempt_model_switch",
    "attempt_summaries",
    "await_quota_ready",
    "awaiting_quota",
    "block",
    "block_switch_failed",
    "cancel",
    "check_acceptance_met",
    "check_create",
    "check_lease",
    "check_reviewer",
    "claim",
    "clear_paused_disk",
    "complete",
    "continuation_of",
    "create_task",
    "edit_task",
    "engine_capacity",
    "engine_choice_for",
    "executor_pin",
    "executor_provider",
    "expire_agent_leases",
    "expire_leases",
    "expire_stalled",
    "expire_timeouts",
    "fail",
    "fail_runtime_holds",
    "flag_quiet",
    "get_task",
    "hold_for_runtime",
    "manages_accounts",
    "mark_paused_disk",
    "moot_decisions",
    "observe",
    "pause_for_quota",
    "planned_state",
    "prefers_local",
    "quiet_facts",
    "quiet_facts_for",
    "record_model_switch",
    "release",
    "release_agent_tasks",
    "release_runtime_holds",
    "renew",
    "renew_paused_disk",
    "report_progress",
    "retry",
    "review",
    "review_escalation",
    "review_failed",
    "set_dependencies",
    "shed_disk_full",
    "stall_cause",
    "task_row",
    "unarchive",
    "unblock",
    "unmet_acceptance",
    "update_continuation",
    "wake_waiting",
]
