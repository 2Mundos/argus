"""Binding a pull request the worker never reported (task deliverable spec 3.7).

The forge is read first, outside any write transaction; the write then records the
PR artifact and its audit event together. Nothing here changes the task's state or
waives a gate: the binding is evidence for the gates that already exist.
"""

import json

import aiosqlite
from argus_core.continuation import (
    ArtifactKind,
    ArtifactState,
    ContinuationArtifact,
    ContinuationProvenance,
    clean,
)
from argus_core.delivery import PrBinding, PrState
from argus_core.tasks import Task

from argus_api.db import Database
from argus_api.errors import ConflictError, ValidationFailedError
from argus_api.events import record
from argus_api.tasks import continuation as cont
from argus_api.tasks import delivery, service

EVENT = "task.pr_reconciled"


async def bind(
    db: Database,
    checker: delivery.PrChecker,
    task_id: str,
    body: PrBinding,
    actor: str,
) -> Task:
    async with db.read() as conn:
        row = await service.task_row(conn, task_id)
        ref = delivery.PrRef(*await _repository(conn, row, body), body.number)
        if await _bound(conn, task_id, ref, body.head_sha):
            return await service.get_task(conn, task_id)
        seen = await checker.snapshot(conn, ref)
    if seen.pr_state is PrState.UNKNOWN or seen.head_sha is None:
        raise ConflictError(
            f"the forge could not confirm pull request {ref.url}",
            code="pr_unverified",
            details=[f"pr_state: {seen.pr_state}", *seen.partial],
        )
    if seen.head_sha != body.head_sha:
        raise ConflictError(
            f"pull request {ref.url} is at {seen.head_sha}, not {body.head_sha}",
            code="pr_stale_head",
            details=[f"forge head: {seen.head_sha}"],
        )
    async with db.write() as tx:
        row = await service.task_row(tx.conn, task_id)
        if await _bound(tx.conn, task_id, ref, body.head_sha):
            return await service.get_task(tx.conn, task_id)
        have = cont.load(row)
        pr = ContinuationArtifact(
            kind=ArtifactKind.PR,
            ref=str(ref.number),
            url=ref.url,
            head_sha=body.head_sha,
            state=ArtifactState(seen.pr_state.value),
            provenance=ContinuationProvenance.ORCHESTRATOR,
            observed_at=tx.now,
            note="bound by reconciliation after a forge read",
        )
        after = await cont.store(
            tx,
            task_id,
            have,
            have.model_copy(
                update={"artifacts": cont.upsert_artifact(have.artifacts, pr)}
            ),
            actor,
            EVENT,
            audit=False,  # the event below says who and why
        )
        await record(
            tx,
            actor,
            "task",
            task_id,
            EVENT,
            {
                "reason": clean(body.reason, 1000),
                "repository": f"https://{ref.host}/{ref.owner}/{ref.repo}",
                "number": ref.number,
                "url": ref.url,
                "head_sha": body.head_sha,
                "pr_state": seen.pr_state.value,
                "task_state": row["state"],
                "revision": after.revision,
            },
        )
        return await service.get_task(tx.conn, task_id)


async def _repository(
    conn: aiosqlite.Connection, row: aiosqlite.Row, body: PrBinding
) -> tuple[str, str, str]:
    """The task project's proven repository, which the caller must have named. Checked
    before the forge read, so a credential never goes to a repository from input."""
    project = row["project"]
    proven = (await delivery.repositories(conn)).get(project)
    if proven is None:
        raise ValidationFailedError(
            f"project {project} has no repository configured",
            code="deliverable_repository",
            details=[f"project: {project}"],
        )
    if delivery.parse_repository(body.repository) != proven:
        raise ValidationFailedError(
            f"{body.repository} is not the repository of project {project}",
            code="pr_wrong_repository",
            details=[f"repository: https://{'/'.join(proven)}"],
        )
    return proven


async def _bound(
    conn: aiosqlite.Connection, task_id: str, ref: delivery.PrRef, head: str
) -> bool:
    """True when this PR is already bound to this head; a PR bound to another head is
    refused rather than rebound."""
    async with conn.execute(
        "SELECT data FROM events WHERE subject_type = 'task' AND subject_id = ?"
        " AND kind = ?",
        (task_id, EVENT),
    ) as cur:
        for (raw,) in await cur.fetchall():
            data = json.loads(raw)
            if data.get("url") != ref.url:
                continue
            if data.get("head_sha") == head:
                return True
            raise ConflictError(
                f"pull request {ref.url} is bound to head {data.get('head_sha')}",
                code="pr_already_bound",
                details=[f"bound head: {data.get('head_sha')}"],
            )
    return False
