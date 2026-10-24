"""A task's delivery as the forge shows it (docs/specs/task-delivery.spec.md)."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class PrState(StrEnum):
    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"
    UNKNOWN = "unknown"  # not read (no credential, network, auth): never "open"


AcceptBlocked = Literal["not_in_review", "checklist_pending", "no_review"]
MERGED_PENDING_REVIEW = "delivery_merged_pending_review"


class Delivery(BaseModel):
    """What Argus read of the task's pull request. Observed only: a merge neither
    closes the task nor counts as acceptance."""

    pr: str  # canonical URL of the pull request
    pr_state: PrState
    merged_at: datetime | None
    checked_at: datetime
    flags: list[Literal["delivery_merged_pending_review"]]
    # The existing review decision, when the state and checklist allow accepting it.
    accept_decision_id: str | None
    # Why accepting is not offered although the PR is merged.
    accept_blocked: AcceptBlocked | None
