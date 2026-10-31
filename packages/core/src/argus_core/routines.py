"""Routines: recurring work on a cron schedule (docs/specs/routines.spec.md)."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from argus_core.tasks import TaskState

Findings = Literal["off", "approval", "auto"]

# Template fields that steer finding intake (3.3); never part of a task body.
INTAKE_FIELDS = frozenset({"dedup_window", "max_findings"})
# The task fields a routine template may set (routines spec 3.1), and the intake's.
TEMPLATE_FIELDS = INTAKE_FIELDS | frozenset(
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


class FindingIntake(BaseModel):
    """The template's intake fields, with their defaults (routines spec 3.3)."""

    model_config = ConfigDict(extra="ignore")

    # Seconds after a finding task closes during which its key stays deduplicated.
    dedup_window: StrictInt = Field(default=7 * 24 * 3600, ge=60, le=365 * 24 * 3600)
    max_findings: StrictInt = Field(default=10, ge=1, le=100)


class Finding(BaseModel):
    """One entry of a run's `result.data.findings`. Untrusted: any other field
    (project, prompt_trust, ...) is ignored; the task create rules apply on top."""

    model_config = ConfigDict(extra="ignore")

    key: StrictStr = Field(min_length=1, max_length=200)
    title: StrictStr
    spec: StrictStr = ""
    acceptance: list[StrictStr] = []
    priority: StrictInt = Field(default=0, ge=-1000, le=1000)


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
