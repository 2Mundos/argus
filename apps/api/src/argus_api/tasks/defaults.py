"""Task defaults: what a new task gets for the fields it leaves out
(docs/specs/task-defaults.spec.md).

One resolution serves creation, draft publish, routine runs and the
effective-defaults read, so they cannot disagree (5).
"""

from collections.abc import Callable
from typing import Any

import aiosqlite
from argus_core.difficulty import Difficulty, DifficultySource
from argus_core.kinds import TaskKind
from argus_core.settings import SettingValue
from argus_core.task_defaults import (
    TaskDefaultProblem,
    TaskDefaults,
    TaskDefaultSource,
    TaskDefaultSources,
    TaskDefaultValues,
)
from argus_core.tasks import Deliverable, TaskCreate

from argus_api.tasks import deliverable

FIELDS = (
    "deliverable",
    "priority",
    "difficulty",
    "required_capabilities",
    "budget_usd",
    "max_attempts",
    "timeout_s",
)
# A request's null means "omitted" for these; for the others it is a value (3.2).
_NULL_IS_OMITTED = frozenset(
    {"deliverable", "priority", "required_capabilities", "max_attempts"}
)
# A null, sent or stored, skips the settings to the kind and built-in fallback; a
# null budget is final: no budget.
_NULL_FALLS_THROUGH = frozenset({"difficulty", "timeout_s"})
_KIND_FIELDS = {
    "required_capabilities": "default_capabilities",
    "max_attempts": "default_max_attempts",
    "timeout_s": "default_timeout_s",
}
_BUILT_IN: dict[str, Any] = {
    "deliverable": Deliverable.RESPONSE,
    "priority": 0,
    "difficulty": None,
    "required_capabilities": [],
    "budget_usd": None,
    "max_attempts": 3,
    "timeout_s": None,
}


def setting_key(field: str) -> str:
    return f"task_default_{field}"


async def stored(
    conn: aiosqlite.Connection, project: str | None
) -> dict[str, SettingValue]:
    # Lazy: the settings service imports knowledge, which imports the task service.
    from argus_api.settings import service as settings_svc

    return await settings_svc.task_defaults(conn, project)


def _coerce(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field == "deliverable":
        return Deliverable(value)
    if field == "difficulty":
        return Difficulty(value)
    if field == "budget_usd":
        return float(value)
    if field == "required_capabilities":
        return list(value)
    return value


def _pick(
    body: TaskCreate,
    kind: TaskKind,
    settings: dict[str, SettingValue],
    field: str,
) -> tuple[Any, TaskDefaultSource] | None:
    """The default for `field` and its source; None when the creator's value stands."""
    value = getattr(body, field)
    sent = field in body.model_fields_set and (
        value is not None or field not in _NULL_IS_OMITTED
    )
    if sent and (value is not None or field not in _NULL_FALLS_THROUGH):
        return None
    setting = settings[setting_key(field)]
    if (
        not sent
        and setting.source in ("project", "global")
        and (setting.value is not None or field not in _NULL_FALLS_THROUGH)
    ):
        return _coerce(field, setting.value), setting.source  # type: ignore[return-value]
    kind_field = _KIND_FIELDS.get(field)
    if kind_field and (kind_value := getattr(kind, kind_field)) is not None:
        return _coerce(field, kind_value), "kind"
    return _BUILT_IN[field], "inferred" if field == "difficulty" else "built_in"


def resolve(
    body: TaskCreate, kind: TaskKind, settings: dict[str, SettingValue]
) -> tuple[TaskCreate, dict[str, TaskDefaultSource]]:
    """Fill every field the creator left out (3.2). Returns the filled body and the
    source of each filled field. The project must already be resolved."""
    update: dict[str, Any] = {}
    sources: dict[str, TaskDefaultSource] = {}
    for field in FIELDS:
        if (picked := _pick(body, kind, settings, field)) is not None:
            update[field], sources[field] = picked
    if sources.get("difficulty") in ("project", "global"):
        update["difficulty_source"] = DifficultySource.DECLARED  # an operator chose it
    return body.model_copy(update=update), sources


def hints(
    project: str | None, sources: dict[str, TaskDefaultSource]
) -> Callable[[deliverable.CreateProblem], list[str]]:
    """Details naming the settings behind a deliverable refusal, and the fix (3.5)."""

    def explain(problem: deliverable.CreateProblem) -> list[str]:
        named = [
            f"{field} came from {setting_key(field)} at"
            f" {'project ' + str(project) if sources[field] == 'project' else 'global'}"
            " scope"
            for field in problem.fields
            if sources.get(field) in ("project", "global")
        ]
        if not named:
            return []
        if problem.code == "deliverable_capability":
            fix = (
                f"fix: add {deliverable.REPOSITORY_WRITE} to"
                " task_default_required_capabilities, or send deliverable or"
                " required_capabilities on the task"
            )
        else:
            fix = (
                "fix: configure the project's repository, set"
                " task_default_deliverable to a non-code deliverable, or send"
                " deliverable on the task"
            )
        return [*named, fix]

    return explain


async def effective(
    conn: aiosqlite.Connection, project: str, kind: TaskKind
) -> TaskDefaults:
    """What a task created in `project` now would get for every omitted field (3.6)."""
    probe = TaskCreate(title="-", project=project, kind=kind.name)
    body, sources = resolve(probe, kind, await stored(conn, project))
    assert body.deliverable is not None and body.required_capabilities is not None
    problems = []
    if problem := await deliverable.creation_problem(
        conn, project, body.required_capabilities, body.deliverable
    ):
        problems.append(
            TaskDefaultProblem(
                code=problem.code, field=problem.fields[-1], message=problem.message
            )
        )
    return TaskDefaults(
        project=project,
        kind=kind.name,
        values=TaskDefaultValues.model_validate(
            {field: getattr(body, field) for field in FIELDS}
        ),
        sources=TaskDefaultSources.model_validate(sources),
        problems=problems,
    )
