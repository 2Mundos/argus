"""Task deliverable rules (docs/specs/task-deliverable.spec.md).

Three checks share one table of evidence: whether a task can be created with its
deliverable, whether a completion carries the evidence Argus holds, and whether a
task that must be green or merged may be accepted. The forge is read by the caller,
outside the write transaction, and handed in; nothing here calls the network.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import aiosqlite
from argus_core.continuation import (
    ArtifactKind,
    ArtifactState,
    Continuation,
    ContinuationArtifact,
    ContinuationProvenance,
)
from argus_core.delivery import CheckState, Delivery, PrState
from argus_core.tasks import (
    CODE_DELIVERABLES,
    FORGE_DELIVERABLES,
    Deliverable,
    TaskResult,
)

from argus_api.db import Tx
from argus_api.errors import ConflictError, ValidationFailedError
from argus_api.events import record
from argus_api.tasks import continuation as cont

REPOSITORY_WRITE = "repository:write"
# Also what an explicit binding (pr_binding.bind) records, so each sees the other's.
BOUND_EVENT = "task.pr_reconciled"
PR_DELIVERABLES = frozenset(
    {Deliverable.PR_OPEN, Deliverable.PR_GREEN, Deliverable.PR_MERGED}
)
_PUSHED = (ArtifactState.OPEN, ArtifactState.MERGED)


@dataclass(frozen=True)
class ForgeRead:
    """What the forge showed for the task's PR before an accept; `delivery` is None
    when the task names no unambiguous PR."""

    delivery: Delivery | None


@dataclass(frozen=True)
class PrMatch:
    """The one PR the forge showed to be this attempt's."""

    repository: str  # https://host/owner/repo
    number: int
    url: str
    head_sha: str
    state: PrState


@dataclass(frozen=True)
class PrSearch:
    """The caller's one automatic search for the PR a completion did not report
    (3.4). `match` is set only when exactly one PR is attributable to the attempt;
    `report` says what was searched and found, for the refusal."""

    match: PrMatch | None
    report: str


@dataclass(frozen=True)
class ForgeGate:
    """The forge's word on the task's PR, read by the caller before the write for a
    completion that must not wait in review (3.4). `state` is `unknown` when no PR
    could be read; `checks` is None when they were not read."""

    state: PrState
    checks: CheckState | None


@dataclass(frozen=True)
class CreateProblem:
    """Why a task with this deliverable could not be created (3.3). `fields` are
    the task fields that combine into the problem."""

    code: str
    message: str
    detail: str
    fields: tuple[str, ...]


async def creation_problem(
    conn: aiosqlite.Connection,
    project: str | None,
    capabilities: list[str],
    level: Deliverable,
) -> CreateProblem | None:
    """A task that can never reach its deliverable is refused up front (3.3)."""
    if level not in CODE_DELIVERABLES:
        return None
    if REPOSITORY_WRITE not in capabilities:
        return CreateProblem(
            code="deliverable_capability",
            message=f"deliverable {level} needs the {REPOSITORY_WRITE} capability",
            detail=f"required_capabilities: {REPOSITORY_WRITE} (deliverable {level})",
            fields=("deliverable", "required_capabilities"),
        )
    # Lazy: the delivery module imports the fleet, which imports the task service.
    from argus_api.tasks import delivery

    if project is not None and project not in await delivery.repositories(conn):
        return CreateProblem(
            code="deliverable_repository",
            message=(
                f"project {project} has no repository configured, so a task cannot"
                f" deliver {level}"
            ),
            detail=f"project: {project} (deliverable {level})",
            fields=("deliverable",),
        )
    return None


async def check_creatable(
    conn: aiosqlite.Connection,
    project: str | None,
    capabilities: list[str],
    level: Deliverable,
    hints: Callable[[CreateProblem], list[str]] | None = None,
) -> None:
    """Raise the creation problem, if any; `hints` add details (task defaults 3.5)."""
    if problem := await creation_problem(conn, project, capabilities, level):
        raise ValidationFailedError(
            problem.message,
            code=problem.code,
            details=[problem.detail, *(hints(problem) if hints else [])],
        )


async def check_complete(
    conn: aiosqlite.Connection,
    row: aiosqlite.Row,
    agent_id: str,
    result: TaskResult,
    have: Continuation,
    search: PrSearch | None = None,
    gate: ForgeGate | None = None,
    acceptance: bool = True,
) -> bool:
    """Refuse a completion without the evidence Argus holds (3.4). True when the
    deliverable also depends on the forge, so the task must wait for review. With
    `gate` (auto-merge, or acceptance off) a green or merged deliverable needs the
    forge's word now, so it never waits; without acceptance and without a gate it
    fails closed."""
    level = Deliverable(row["deliverable"])
    advice = await _missing(conn, row, agent_id, result, have, level, search)
    if advice is None and level in FORGE_DELIVERABLES and (gate or not acceptance):
        advice = _forge_advice(level, gate)
        if advice is None:
            return False
    if advice:
        raise ValidationFailedError(
            f"deliverable {level} is not met: {advice}",
            code="deliverable_missing",
            details=[advice],
        )
    return level in FORGE_DELIVERABLES


def _forge_advice(level: Deliverable, gate: ForgeGate | None) -> str | None:
    """What the worker must do before completing, or None when the forge confirms."""
    if gate is None or gate.state is PrState.UNKNOWN:
        return "the forge could not confirm the pull request; complete again"
    if gate.state is PrState.MERGED:
        return None
    if level is Deliverable.PR_MERGED:
        return (
            "merge the pull request once its checks pass, then complete again;"
            f" the forge shows it {gate.state}, not merged"
        )
    if gate.state is PrState.OPEN and gate.checks is CheckState.SUCCESS:
        return None
    return (
        "make the checks of the pull request head pass, then complete again;"
        f" the pull request is {gate.state}, checks {gate.checks or 'unread'}"
    )


def _has_artifact(have: Continuation, kind: ArtifactKind) -> bool:
    return any(a.kind is kind and a.state in _PUSHED for a in have.artifacts)


def has_pr(have: Continuation) -> bool:
    return _has_artifact(have, ArtifactKind.PR)


async def bind_found(
    tx: Tx,
    row: aiosqlite.Row,
    have: Continuation,
    search: PrSearch | None,
    actor: str,
) -> Continuation:
    """Record the PR the caller's search matched as host-derived evidence, unless the
    worker already reported one (an explicit report comes first). Evidence only: the
    forge state and checks still decide the gate. A replay finds the artifact and
    changes nothing."""
    if (
        search is None
        or search.match is None
        or Deliverable(row["deliverable"]) not in PR_DELIVERABLES
        or has_pr(have)
    ):
        return have
    pr = search.match
    artifact = ContinuationArtifact(
        kind=ArtifactKind.PR,
        ref=str(pr.number),
        url=pr.url,
        head_sha=pr.head_sha,
        state=ArtifactState(pr.state.value),
        provenance=ContinuationProvenance.ARGUS,
        observed_at=tx.now,
        attempt=row["attempt"],
        note="bound by Argus from a forge read at completion; not reported by the worker",
    )
    after = await cont.store(
        tx,
        row["id"],
        have,
        have.model_copy(
            update={"artifacts": cont.upsert_artifact(have.artifacts, artifact)}
        ),
        actor,
        BOUND_EVENT,
        audit=False,  # the event below says who and why
    )
    await record(
        tx,
        actor,
        "task",
        row["id"],
        BOUND_EVENT,
        {
            "reason": "bound at completion from a forge read; the worker reported no PR",
            "source": "completion",
            "repository": pr.repository,
            "number": pr.number,
            "url": pr.url,
            "head_sha": pr.head_sha,
            "pr_state": pr.state.value,
            "task_state": row["state"],
            "revision": after.revision,
        },
    )
    return after


async def _missing(
    conn: aiosqlite.Connection,
    row: aiosqlite.Row,
    agent_id: str,
    result: TaskResult,
    have: Continuation,
    level: Deliverable,
    search: PrSearch | None,
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
            if _has_artifact(have, ArtifactKind.BRANCH) or _has_artifact(
                have, ArtifactKind.PR
            ):
                return None
            return (
                "push the branch and report it (report_observations: artifact"
                " kind branch, state open)"
            )
        case _:  # pr_open, pr_green, pr_merged
            if _has_artifact(have, ArtifactKind.PR):
                return None
            advice = (
                "open a pull request and report it (progress kind pr_opened, or"
                " report_observations) before completing"
            )
            return f"{advice}. {search.report}" if search else advice


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
