"""Task deliverable rules (docs/specs/task-deliverable.spec.md).

Three checks share one table of evidence: whether a task can be created with its
deliverable, whether a completion carries the evidence Argus holds, and whether a
task that must be green or merged may be accepted. The forge is read by the caller,
outside the write transaction, and handed in; nothing here calls the network.
"""

from __future__ import annotations

from dataclasses import dataclass

import aiosqlite
from argus_core.continuation import (
    ArtifactKind,
    ArtifactState,
    Continuation,
)
from argus_core.delivery import CheckState, Delivery, PrState
from argus_core.tasks import (
    CODE_DELIVERABLES,
    FORGE_DELIVERABLES,
    Deliverable,
    TaskResult,
)

from argus_api.errors import ConflictError, ValidationFailedError

REPOSITORY_WRITE = "repository:write"
_PUSHED = (ArtifactState.OPEN, ArtifactState.MERGED)


@dataclass(frozen=True)
class ForgeRead:
    """What the forge showed for the task's PR before an accept; `delivery` is None
    when the task names no unambiguous PR."""

    delivery: Delivery | None


async def check_creatable(
    conn: aiosqlite.Connection,
    project: str | None,
    capabilities: list[str],
    level: Deliverable,
) -> None:
    """A task that can never reach its deliverable is refused up front (3.3)."""
    if level not in CODE_DELIVERABLES:
        return
    if REPOSITORY_WRITE not in capabilities:
        raise ValidationFailedError(
            f"deliverable {level} needs the {REPOSITORY_WRITE} capability",
            code="deliverable_capability",
            details=[
                f"required_capabilities: {REPOSITORY_WRITE} (deliverable {level})"
            ],
        )
    # Lazy: the delivery module imports the fleet, which imports the task service.
    from argus_api.tasks import delivery

    if project is not None and project not in await delivery.repositories(conn):
        raise ValidationFailedError(
            f"project {project} has no repository configured, so a task cannot"
            f" deliver {level}",
            code="deliverable_repository",
            details=[f"project: {project} (deliverable {level})"],
        )


async def check_complete(
    conn: aiosqlite.Connection,
    row: aiosqlite.Row,
    agent_id: str,
    result: TaskResult,
    cont: Continuation,
) -> bool:
    """Refuse a completion without the evidence Argus holds (3.4). True when the
    deliverable also depends on the forge, so the task must wait for review."""
    level = Deliverable(row["deliverable"])
    if advice := await _missing(conn, row, agent_id, result, cont, level):
        raise ValidationFailedError(
            f"deliverable {level} is not met: {advice}",
            code="deliverable_missing",
            details=[advice],
        )
    return level in FORGE_DELIVERABLES


def _has_artifact(cont: Continuation, kind: ArtifactKind) -> bool:
    return any(a.kind is kind and a.state in _PUSHED for a in cont.artifacts)


async def _missing(
    conn: aiosqlite.Connection,
    row: aiosqlite.Row,
    agent_id: str,
    result: TaskResult,
    cont: Continuation,
    level: Deliverable,
) -> str | None:
    """What to do, or None when the evidence is there."""
    match level:
        case Deliverable.RESPONSE:
            return None
        case Deliverable.KNOWLEDGE:
            if await _wrote_knowledge(conn, row, f"agent:{agent_id}"):
                return None
            return "write a knowledge page (write_knowledge) before completing"
        case Deliverable.ARTIFACT:
            if result.artifacts:
                return None
            return "attach an artifact and list it in result.artifacts"
        case Deliverable.SUBTASKS:
            async with conn.execute(
                "SELECT 1 FROM tasks WHERE parent_id = ? AND state != 'cancelled'",
                (row["id"],),
            ) as cur:
                if await cur.fetchone():
                    return None
            return "create at least one subtask before completing"
        case Deliverable.BRANCH:
            if _has_artifact(cont, ArtifactKind.BRANCH) or _has_artifact(
                cont, ArtifactKind.PR
            ):
                return None
            return (
                "push the branch and report it (report_observations: artifact"
                " kind branch, state open)"
            )
        case _:  # pr_open, pr_green, pr_merged
            if _has_artifact(cont, ArtifactKind.PR):
                return None
            return (
                "open a pull request and report it (progress kind pr_opened, or"
                " report_observations) before completing"
            )


async def _wrote_knowledge(
    conn: aiosqlite.Connection, row: aiosqlite.Row, actor: str
) -> bool:
    """The agent published or proposed a page since it claimed this attempt."""
    async with conn.execute(
        "SELECT claimed_at FROM task_attempts WHERE task_id = ? AND attempt = ?",
        (row["id"], row["attempt"]),
    ) as cur:
        attempt = await cur.fetchone()
    if attempt is None:
        return False
    since = attempt["claimed_at"]
    async with conn.execute(
        "SELECT 1 FROM knowledge_revisions WHERE author = ? AND created_at >= ?"
        " UNION ALL SELECT 1 FROM knowledge_proposals"
        " WHERE author = ? AND created_at >= ? AND state != 'withdrawn' LIMIT 1",
        (actor, since, actor, since),
    ) as cur:
        return await cur.fetchone() is not None


def check_accept(row: aiosqlite.Row, forge: ForgeRead | None) -> None:
    """A task that must be green or merged is accepted only when the forge shows it
    (3.5). Without a read the accept fails closed."""
    level = Deliverable(row["deliverable"])
    if level not in FORGE_DELIVERABLES:
        return
    if forge is None:
        raise _unverified("the forge was not read for this accept")
    seen = forge.delivery
    if seen is None:
        raise _unverified("the task names no unambiguous pull request")
    if seen.pr_state is PrState.UNKNOWN:
        raise _unverified("the forge could not confirm the pull request")
    if seen.pr_state is PrState.MERGED:
        return
    if level is Deliverable.PR_MERGED or seen.pr_state is not PrState.OPEN:
        raise _unmet(level, f"the pull request is {seen.pr_state}, not merged")
    checks = seen.checks_combined
    if checks is CheckState.SUCCESS:
        return
    if checks is None or checks is CheckState.UNKNOWN:
        raise _unverified("the checks of the pull request head are unknown")
    raise _unmet(level, f"the checks of the pull request head are {checks}")


def _unverified(why: str) -> ConflictError:
    return ConflictError(
        f"the deliverable cannot be confirmed: {why}",
        code="deliverable_unverified",
        details=[why],
    )


def _unmet(level: Deliverable, why: str) -> ConflictError:
    return ConflictError(
        f"deliverable {level} is not met: {why}",
        code="deliverable_unmet",
        details=[why],
    )
