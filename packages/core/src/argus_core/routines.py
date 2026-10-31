"""Routines: recurring work on a cron schedule (docs/specs/routines.spec.md)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from argus_core.tasks import TaskState

Findings = Literal["off", "approval", "auto"]

# The task fields a routine template may set (routines spec 3.1).
TEMPLATE_FIELDS = frozenset(
    {
        "kind",
        "title",
        "spec",
        "acceptance",
        "input",
        "required_capabilities",
        "priority",
        "review",
        "difficulty",
        "harness_model",
        "executor_model",
    }
)


class RoutineWrite(BaseModel):
    """`PUT /projects/{slug}/routines/{routine}`: the whole routine. `project` and
    `slug` are optional echoes of the path and must match it."""

    model_config = ConfigDict(extra="forbid")

    project: str | None = None
    slug: str | None = None
    title: str = Field(min_length=1, max_length=200)
    schedule: str = Field(min_length=1, max_length=200)
    timezone: str = Field(default="UTC", min_length=1, max_length=100)
    template: dict[str, Any] = {}
    findings: Findings = "approval"
    enabled: bool = True


class RoutineEnabled(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class RoutineRun(BaseModel):
    task_id: str
    state: TaskState
    created_at: datetime


class Routine(BaseModel):
    id: str
    project: str
    slug: str
    title: str
    schedule: str
    timezone: str
    template: dict[str, Any]
    findings: Findings
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_task_id: str | None
    created_by: str
    created_at: datetime
    updated_at: datetime
    # The next three slots from now, enabled or not: a preview of the schedule.
    next_slots: list[datetime]
    # Most recent run tasks first; filled by `GET /routines/{id}` only.
    last_runs: list[RoutineRun] = []


class RoutineRunResult(BaseModel):
    """`POST /routines/{id}/run`: the run task, or why none was created."""

    routine_id: str
    task_id: str | None  # None when skipped
    skipped: Literal["previous_running"] | None = None
