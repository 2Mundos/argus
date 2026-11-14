"""Reason strings a task is held, blocked or failed with, shared by the task modules."""

# `blocked` for want of a usable model account (account selection 3.6). Rows written
# before the rename carry `no_account`; both are read and released alike.
AWAITING_QUOTA = "awaiting_quota"
# Admission failures end an attempt without spending it: the task never ran.
# `account_unavailable` is a lease or apply error that is not a provider limit.
ADMISSION_REASONS = frozenset({"quota", "account_unavailable"})
QUOTA_WAIT_REASONS = (AWAITING_QUOTA, "no_account")

# `blocked` while the project's image is prepared / after its preparation failed
# (project runtime spec 3.4): neither spends `failures` or `max_attempts`.
RUNTIME_PREPARING = "runtime_preparing"
RUNTIME_FAILED = "runtime_failed"
# Prefix of the progress note on a ready task no live active worker can run.
NO_COMPATIBLE_WORKER = "no compatible worker: "
# `blocked` after the worker said FAIL with attempts left (failure classes 3.2).
WORKER_GAVE_UP = "worker_gave_up"


RUNTIME_SWITCH_FAILED = "runtime_switch_failed"
