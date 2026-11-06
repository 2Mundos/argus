"""What a new task gets for the fields it leaves out (docs/specs/task-defaults.spec.md)."""

from typing import Literal

from pydantic import BaseModel

from argus_core.difficulty import Difficulty
from argus_core.tasks import Deliverable

# Where a resolved value came from (3.2): a project or global setting, the kind,
# the built-in fallback, or (difficulty) inference from the title and spec.
TaskDefaultSource = Literal["project", "global", "kind", "built_in", "inferred"]


class TaskDefaultValues(BaseModel):
    deliverable: Deliverable
    priority: int
    difficulty: Difficulty | None  # None: inferred per task
    required_capabilities: list[str]
    budget_usd: float | None  # None: no budget
    max_attempts: int
    timeout_s: int | None  # None: the server's ARGUS_TASK_TIMEOUT_S at claim


class TaskDefaultSources(BaseModel):
    deliverable: TaskDefaultSource
    priority: TaskDefaultSource
    difficulty: TaskDefaultSource
    required_capabilities: TaskDefaultSource
    budget_usd: TaskDefaultSource
    max_attempts: TaskDefaultSource
    timeout_s: TaskDefaultSource


class TaskDefaultProblem(BaseModel):
    """A combination creation would refuse, e.g. `deliverable_capability`."""

    code: str
    field: str
    message: str


class TaskDefaults(BaseModel):
    project: str
    kind: str
    values: TaskDefaultValues
    sources: TaskDefaultSources
    problems: list[TaskDefaultProblem]
