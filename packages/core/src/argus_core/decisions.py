"""Decision requests: one primitive for every human/orchestrator gate (3.10)."""

from datetime import datetime
from enum import StrEnum
from typing import Any

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


class Ask(BaseModel):
    attempt: int
    question: str = Field(min_length=1, max_length=10_000)
    options: list[str] | None = Field(default=None, min_length=2)
    audience: Audience = Audience.ANY
