"""Decision requests: one primitive for every human/orchestrator gate (3.10)."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from argus_core.knowledge import SaveToKnowledge


class DecisionKind(StrEnum):
    QUESTION = "question"  # a worker asked; the answer goes back into the task
    APPROVAL = "approval"  # start gate for requires_approval tasks
    REVIEW = "review"  # review gate after completion
    POLICY = "policy"  # a policy limit was hit; approve to proceed
    KNOWLEDGE = "knowledge"  # a worker proposed a knowledge change


class Audience(StrEnum):
    HUMAN = "human"  # admin tokens
    ORCHESTRATOR = "orchestrator"  # orchestrators (and admins)
    ANY = "any"  # orchestrators or admins


# The review gate's choices (task deliverable spec 3.10). `accept_with_deviation`
# succeeds over unmet acceptance items, which stay `verification_missing`, and needs
# the answer text to record the deviation and who authorized it. Review decisions
# opened with only accept/reject take it too.
ACCEPT_WITH_DEVIATION = "accept_with_deviation"
REVIEW_OPTIONS = ("accept", ACCEPT_WITH_DEVIATION, "reject")

# The review decision opened when no independent reviewer could deliver a verdict
# (settings spec 3.5): its payload is `{"gate": "review_failed"}` and its choices are
# these, not REVIEW_OPTIONS.
REVIEW_FAILED_GATE = "review_failed"
RETRY_REVIEWER = "retry_reviewer"
APPROVE_WITHOUT_REVIEW = "approve_without_review"
REVIEW_FAILED_OPTIONS = (RETRY_REVIEWER, APPROVE_WITHOUT_REVIEW, "cancel")

# A task in `in_review` always has an active reviewer or an open review decision.
# When the independent review cannot deliver, the regular review decision (REVIEW_OPTIONS)
# opens with one of these `gate` values in its payload (settings spec 3.5).
NO_REVIEWER_GATE = "no_reviewer"
REVIEWER_FAILED_GATE = "reviewer_failed"
GATE_DISABLED_GATE = "gate_disabled"
# The review decision opened when independent review exhausted `max_rounds` with the
# newest review still requesting changes. Its payload is
# `{"gate": "rounds_exhausted", "child_id": ..., "rounds": N}`.
ROUNDS_EXHAUSTED_GATE = "rounds_exhausted"
# The review decision opened after an approval of a head the review policy sorts as
# `human`: its payload is `{"gate": "policy_human", "rules", "pr", "head_sha"}`
# (review policy spec 3.4).
POLICY_HUMAN_GATE = "policy_human"


class DecisionState(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    CANCELLED = "cancelled"  # moot: the subject moved on without an answer


class Decision(BaseModel):
    id: str
    subject_type: str
    subject_id: str
    kind: DecisionKind
    question: str
    options: list[str] | None
    audience: Audience
    state: DecisionState
    answer_choice: str | None
    answer_text: str | None
    requested_by: str
    decided_by: str | None
    created_at: datetime
    decided_at: datetime | None
    subject_title: str | None = None  # the task's title, for display
    project: str | None = None  # the subject's project; None for pools
    payload: dict[str, Any] | None = None  # structured context, e.g. a requested scale
    request_id: str | None = None  # the answering caller's, for safe retries


class DecisionAnswer(BaseModel):
    choice: str | None = None  # required when the decision has options
    text: str = Field(default="", max_length=10_000)
    save_to_knowledge: SaveToKnowledge | None = None  # questions only
    # Retrying with the same id, choice and text replays the stored answer.
    request_id: str | None = Field(default=None, min_length=1, max_length=200)


# The typed `resource` of an assistance question that only asks for a pull request to
# be merged (task delivery spec 3.7). Only this exact resource lets Argus close the
# question when the PR merges elsewhere; any other text stays for the orchestrator.
MERGE_ASSISTANCE_RESOURCE = "pr-merge"


class Ask(BaseModel):
    attempt: int
    question: str = Field(min_length=1, max_length=10_000)
    options: list[str] | None = Field(default=None, min_length=2)
    audience: Audience = Audience.ANY
    # `assistance`: a technical blocker (a tool, credential or service is missing)
    # asked of the orchestrator, never a decision about the goal. It cannot target a
    # human audience; `resource` names what is missing.
    category: Literal["decision", "assistance"] = "decision"
    resource: str | None = Field(default=None, min_length=1, max_length=100)
