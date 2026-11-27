"""What will decide whether a task can finish, as facts on the task read
(worker lifecycle spec 3.7).

These are DECISIONS, not hints. `decide_merge` is the Python twin of
`taskAuthorizesMerge` / `taskDeniesMerge` / `taskMayMerge` (integrations/nanoclaw/argus-merge-auth.ts, which
stays authoritative) and `forge_review_policy` of `forgeReviewPolicy`
(argus-forge-review.ts). Both sides run the cases in
integrations/nanoclaw/fixtures/merge-decision/cases.json, so a parity break fails in both
languages. The claim delivers the same two inputs (`auto_merge`,
`forge_agentic_review_required`), resolved by the same settings functions.
"""

from typing import Literal

from argus_core.delivery import CheckState, Delivery, PrState
from argus_core.settings import SettingValue
from argus_core.tasks import Deliverable, PromptTrust, Task
from pydantic import BaseModel

ForgeReview = Literal["required", "off"]


class MergeDecision(BaseModel):
    """Whether this task's own merge is authorized, and what decided it."""

    authorized: bool
    # Authorized and `repository:write`: a read-only task never merges (taskMayMerge).
    may_merge: bool
    deliverable: Deliverable
    # Effective `delivery.auto_merge` and the layer that set it.
    auto_merge: bool
    auto_merge_source: Literal["project", "global", "default"]
    # `input.delivery.allow_merge`; false when prompt_trust is "worker" (never authority).
    allow_merge: bool
    # Owner-authored `input.delivery.deny_merge: true`: vetoes every grant (never worker input).
    deny_merge: bool
    # Which input granted the merge, when one did.
    granted_by: Literal["auto_merge", "allow_merge"] | None


def decide_merge(
    task: Task,
    auto_merge: SettingValue,
    *,
    independent_review_required: bool = False,
    independent_review_approved: bool = False,
) -> MergeDecision:
    enabled = bool(auto_merge.value)
    by_setting = enabled and task.deliverable is Deliverable.PR_MERGED
    delivery = task.input.get("delivery") if isinstance(task.input, dict) else None
    allow = (
        task.prompt_trust is not PromptTrust.WORKER
        and isinstance(delivery, dict)
        and delivery.get("allow_merge") is True
    )
    deny = (
        task.prompt_trust is not PromptTrust.WORKER
        and isinstance(delivery, dict)
        and delivery.get("deny_merge") is True
    )
    by_setting = by_setting and not deny
    allow = allow and not deny
    authorized = by_setting or allow
    review_ok = not independent_review_required or independent_review_approved
    return MergeDecision(
        authorized=authorized,
        may_merge=authorized
        and review_ok
        and "repository:write" in task.required_capabilities,
        deliverable=task.deliverable,
        auto_merge=enabled,
        auto_merge_source="project"
        if auto_merge.source == "project"
        else ("global" if auto_merge.source == "global" else "default"),
        allow_merge=allow,
        deny_merge=deny,
        granted_by="auto_merge" if by_setting else ("allow_merge" if allow else None),
    )


def forge_review_policy(required: bool) -> ForgeReview:
    return "required" if required else "off"


class PrGate(BaseModel):
    """What the PR readiness gate (`argus-task pr-ready`) would report, from the
    delivery snapshot rather than a fresh forge read: CI reasons use its codes; the
    review reason counts forge reviews on the head, not the agentic review comment."""

    pr: str
    pr_state: PrState
    head_sha: str | None
    ready: bool
    reasons: list[str]
    review_policy: ForgeReview
    approvals_for_head: int | None


_CI_REASONS = {
    CheckState.PENDING: "ci_running",
    CheckState.FAILURE: "ci_failed",
    CheckState.UNKNOWN: "ci_unknown",
}


def pr_gate(delivery: Delivery, review: ForgeReview) -> PrGate:
    reasons: list[str] = []
    approvals = delivery.review.forge_reviews_for_head if delivery.review else None
    if delivery.pr_state is PrState.OPEN:
        if not delivery.checks:
            reasons.append("ci_missing")
        elif delivery.checks_combined in _CI_REASONS:
            reasons.append(_CI_REASONS[delivery.checks_combined])
        if review == "required" and not approvals:
            reasons.append("review_required")
        if delivery.merge and delivery.merge.mergeable is False:
            reasons.append("not_mergeable")
    return PrGate(
        pr=delivery.pr,
        pr_state=delivery.pr_state,
        head_sha=delivery.head.sha if delivery.head else None,
        ready=delivery.pr_state is PrState.OPEN and not reasons,
        reasons=reasons,
        review_policy=review,
        approvals_for_head=approvals,
    )
