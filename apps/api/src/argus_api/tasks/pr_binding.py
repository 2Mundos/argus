"""Binding a pull request the worker never reported (task deliverable spec 3.7), and
taking a task that failed for want of it to review once it merged (3.8).

The forge is read first, outside any write transaction; the write then re-checks the
task and records the change with its audit event. The binding changes no state; the
delivery reconciliation only opens the normal review decision, so no gate is waived.
"""

import json
import re

import aiosqlite
from argus_core.continuation import (
    ArtifactKind,
    ArtifactState,
    ContinuationArtifact,
    ContinuationProvenance,
    clean,
)
from argus_core.delivery import DeliveryReconciliation, PrBinding, PrState
from argus_core.tasks import Task

from argus_api.db import Database
from argus_api.errors import ConflictError, ValidationFailedError
from argus_api.events import record
from argus_api.tasks import continuation as cont
from argus_api.tasks import delivery, service

EVENT = "task.pr_reconciled"
DELIVERY_EVENT = "task.delivery_reconciled"
# What the channel sends when Argus refuses a COMPLETE for want of the deliverable
# (failure classes 3.1: a `channel_error` fail). Its text is fixed by the channel.
_DELIVERABLE_MISSING = re.compile(r"\(422 deliverable_missing\)")


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


async def reconcile_delivery(
    db: Database,
    checker: delivery.PrChecker,
    task_id: str,
    body: DeliveryReconciliation,
    actor: str,
) -> Task:
    """`failed` (deliverable_missing) -> `in_review` when the bound PR merged at the
    named head. Every check runs again inside the write, so of concurrent requests
    exactly one moves the task and the others replay."""
    async with db.read() as conn:
        row = await service.task_row(conn, task_id)
        ref = delivery.PrRef(*await _repository(conn, row, body), body.number)
        if await _reconciled(conn, task_id, ref, body):
            return await service.get_task(conn, task_id)
        await _eligible(conn, row, ref, body)
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
    if seen.pr_state is not PrState.MERGED:
        raise ConflictError(
            f"pull request {ref.url} is {seen.pr_state}, not merged",
            code="pr_not_merged",
            details=[f"pr_state: {seen.pr_state}"],
        )
    if seen.merge_sha is None:
        raise ConflictError(
            f"the forge reports no merge commit for {ref.url}",
            code="pr_unverified",
            details=["merge_commit_sha: missing", *seen.partial],
        )
    async with db.write() as tx:
        row = await service.task_row(tx.conn, task_id)
        if await _reconciled(tx.conn, task_id, ref, body):
            return await service.get_task(tx.conn, task_id)
        failure = await _eligible(tx.conn, row, ref, body)
        branches = {
            a.ref for a in cont.load(row).artifacts if a.kind is ArtifactKind.BRANCH
        }
        if branches and seen.head_ref not in branches:
            raise ConflictError(
                f"pull request {ref.url} is from {seen.head_ref}, not the task's branch",
                code="pr_branch_mismatch",
                details=[f"task branch: {b}" for b in sorted(branches)],
            )
        return await service.review_failed(
            tx,
            row,
            actor,
            DELIVERY_EVENT,
            {
                "reason": clean(body.reason, 1000),
                "failure_event_id": body.failure_event_id,
                "failure_reason": failure.get("reason"),
                "failure_class": "deliverable_missing",
                "repository": f"https://{ref.host}/{ref.owner}/{ref.repo}",
                "number": ref.number,
                "url": ref.url,
                "head_sha": body.head_sha,
                "head_ref": seen.head_ref,
                "merge_sha": seen.merge_sha,
                "merged_at": seen.merged_at.isoformat() if seen.merged_at else None,
                "revision": cont.load(row).revision,
            },
        )


async def _eligible(
    conn: aiosqlite.Connection,
    row: aiosqlite.Row,
    ref: delivery.PrRef,
    body: DeliveryReconciliation,
) -> dict:
    """The facts Argus holds, checked before the forge read and again in the write:
    the failure the caller named is the current one and is `deliverable_missing`, and
    the named PR is the one bound to the task at the named head. Returns that
    failure's event data."""
    if row["state"] != "failed":
        raise ConflictError(
            f"task is {row['state']}, not failed", code="task_not_failed"
        )
    async with conn.execute(
        "SELECT id, data FROM events WHERE subject_type = 'task' AND subject_id = ?"
        " AND json_extract(data, '$.to') = 'failed' ORDER BY id DESC LIMIT 1",
        (row["id"],),
    ) as cur:
        found = await cur.fetchone()
    if (
        found is None
        or found[0] != body.failure_event_id
        or row["attempt"] != body.attempt
    ):
        raise ConflictError(
            "the task's failure is not the one named",
            code="stale_failure",
            details=[
                f"attempt: {row['attempt']}",
                f"failure_event_id: {found[0] if found else None}",
            ],
        )
    failure = json.loads(found[1])
    if failure.get("reason") != "channel_error" or not _DELIVERABLE_MISSING.search(
        str(failure.get("error") or "")
    ):
        raise ConflictError(
            "the task did not fail for a missing deliverable",
            code="failure_not_deliverable_missing",
            details=[f"failure reason: {failure.get('reason')}"],
        )
    bound = await _bound_heads(conn, row["id"], ref)
    if body.head_sha not in bound:
        raise ConflictError(
            f"pull request {ref.url} is not bound at {body.head_sha}",
            code="pr_stale_head" if bound else "pr_not_bound",
            details=[f"bound head: {h}" for h in bound]
            or ["bind it first with reconcile-pr"],
        )
    repos = await delivery.repositories(conn)
    others = {
        r
        for r in [
            delivery.extract([], [a], repos.get(row["project"]), set(repos.values()))
            for a in cont.load(row).artifacts
            if a.kind is ArtifactKind.PR
        ]
        if r != ref
    }
    if others:
        raise ConflictError(
            "the task holds more than one pull request",
            code="pr_ambiguous",
            details=[f"also: {r.url if r else 'an unresolved PR'}" for r in others],
        )
    return failure


async def _bound_heads(
    conn: aiosqlite.Connection, task_id: str, ref: delivery.PrRef
) -> list[str]:
    async with conn.execute(
        "SELECT data FROM events WHERE subject_type = 'task' AND subject_id = ?"
        " AND kind = ?",
        (task_id, EVENT),
    ) as cur:
        rows = [json.loads(raw) for (raw,) in await cur.fetchall()]
    return [d["head_sha"] for d in rows if d.get("url") == ref.url]


async def _reconciled(
    conn: aiosqlite.Connection,
    task_id: str,
    ref: delivery.PrRef,
    body: DeliveryReconciliation,
) -> bool:
    """True when this failure was already reconciled with this PR and head (a replay);
    another PR or head for the same failure is refused."""
    async with conn.execute(
        "SELECT data FROM events WHERE subject_type = 'task' AND subject_id = ?"
        " AND kind = ?",
        (task_id, DELIVERY_EVENT),
    ) as cur:
        for (raw,) in await cur.fetchall():
            data = json.loads(raw)
            if data.get("failure_event_id") != body.failure_event_id:
                continue
            if (data.get("url"), data.get("head_sha")) == (ref.url, body.head_sha):
                return True
            raise ConflictError(
                "this failure was reconciled with another pull request or head",
                code="delivery_already_reconciled",
                details=[f"url: {data.get('url')}", f"head: {data.get('head_sha')}"],
            )
    return False
