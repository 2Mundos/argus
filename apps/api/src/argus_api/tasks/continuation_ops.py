"""Continuations: clarifications, observed merges and continuation updates.

See docs/specs/task-continuation.spec.md.
"""

import contextlib
import json
from datetime import datetime

import aiosqlite
from argus_core.continuation import (
    ArtifactKind,
    ArtifactState,
    ChecklistItem,
    Continuation,
    ContinuationArtifact,
    ContinuationProvenance,
    ContinuationUpdate,
    ContinuationWip,
    EffectiveContinuation,
    EnvCheck,
    ItemStatus,
    ObservationRequest,
    clean,
    merge_acceptance,
)
from argus_core.tasks import Task

from argus_api.db import Tx, ts
from argus_api.errors import ConflictError, RateLimitedError, ValidationFailedError
from argus_api.tasks import continuation as cont
from argus_api.tasks import steers
from argus_api.tasks.edit import _digest
from argus_api.tasks.leases import _owned
from argus_api.tasks.reads import _row, get_task


async def add_clarification(
    tx: Tx,
    task_id: str,
    question: str,
    answer: str,
    actor: str,
    *,
    decision_id: str | None = None,
    choice: str | None = None,
    text: str | None = None,
    options: list[str] | None = None,
    category: str = "decision",
) -> None:
    """Append an answered question to the task spec (audit narrative the next claim
    sees as history) and record it as an effective decision with scope and time. The
    structured decision, not this text, is what a later attempt treats as current."""
    row = await _row(tx.conn, task_id)
    spec = row["spec"]
    if "\n## Clarifications\n" not in f"\n{spec}":
        spec += "\n\n## Clarifications\n"
    label = "Assistance" if category == "assistance" else "Answered"
    spec += f"\n- **Asked (attempt {row['attempt']}):** {question}\n"
    spec += f"  **{label} by {actor} ({ts(tx.now)}):** {answer}\n"
    await tx.conn.execute(
        "UPDATE tasks SET spec = ?, updated_at = ? WHERE id = ?",
        (spec, ts(tx.now), task_id),
    )
    if decision_id is not None:
        await cont.on_decision(
            tx,
            task_id,
            decision_id=decision_id,
            question=question,
            choice=choice,
            text=text if text is not None else answer,
            options=options,
            category=category,
            actor=actor,
            attempt=row["attempt"],
        )


async def answered_equivalent(tx: Tx, task_id: str, question: str):
    """An effective earlier answer to this very question, if any."""
    return await cont.answered_equivalent(tx, task_id, question)


async def continuation_of(
    conn: aiosqlite.Connection, task_id: str
) -> EffectiveContinuation:
    """The effective contract of a task (stored facts plus derived view)."""
    task = await get_task(conn, task_id)
    assert task.continuation is not None
    return task.continuation


def _artifact(
    a, provenance: ContinuationProvenance, at: datetime, attempt: int | None
) -> ContinuationArtifact:
    return ContinuationArtifact(
        **a.model_dump(exclude={"note"}),
        note=clean(a.note, 1000),
        provenance=provenance,
        observed_at=at,
        attempt=attempt,
    )


MERGE_STEER = (
    "The pull request for this task is merged. A merge is not acceptance: the "
    "checklist, evidence and review requirements still apply. Re-check them and "
    "finish the task."
)


async def observe(
    tx: Tx, agent_id: str, task_id: str, body: ObservationRequest
) -> Task:
    """The holder's runtime reports what it saw of the live system (PR state, head,
    environment readiness). The runtime made those calls before this transaction; here
    they are only stored. Idempotent: repeating an observation changes nothing, and an
    older one never overwrites a newer. Contradictions with the record become bounded
    `reconciliation` entries instead of silent overwrites."""
    row = await _owned(tx, task_id, body.attempt, agent_id)
    have = cont.load(row)
    seen = [
        _artifact(a, ContinuationProvenance.RUNTIME, tx.now, body.attempt)
        for a in body.artifacts
    ]
    artifacts = have.artifacts
    for a in seen:
        artifacts = cont.upsert_artifact(artifacts, a)
    wip = have.wip
    if body.wip is not None:
        # A required input is the orchestrator's to name or replace (task inputs 3.6).
        if any(
            (w.ref or "").startswith(cont.INPUT_REF_PREFIX)
            for w in (body.wip, have.wip)
            if w is not None
        ):
            raise ValidationFailedError(
                "only an orchestrator or admin may set or replace a required input",
                code="input_ref_forbidden",
            )
        wip = ContinuationWip(
            summary=clean(body.wip.summary, 1000) or "",
            ref=clean(body.wip.ref, 250),
            provenance=ContinuationProvenance.WORKER,
            observed_at=tx.now,
        )
    env = cont.apply_environment(
        have.environment,
        [
            EnvCheck(**e.model_dump(), provenance=ContinuationProvenance.RUNTIME)
            for e in body.environment
        ],
        tx.now,
    )
    want = have.model_copy(
        update={
            "artifacts": artifacts,
            "wip": wip,
            "environment": env,
            "reconciliation": cont.reconcile(have, seen, tx.now),
        }
    )
    stored = await cont.store(
        tx, task_id, have, want, f"agent:{agent_id}", "observation"
    )
    if _newly_merged(have, stored):
        await _announce_merge(tx, row, f"agent:{agent_id}")
    return await get_task(tx.conn, task_id)


def _newly_merged(have: Continuation, now: Continuation) -> bool:
    """A pull request the record held as anything but merged is merged now."""
    before = {a.key: a.state for a in have.artifacts}
    return any(
        a.kind is ArtifactKind.PR
        and a.state is ArtifactState.MERGED
        and before.get(a.key) is not ArtifactState.MERGED
        for a in now.artifacts
    )


async def _announce_merge(tx: Tx, row: aiosqlite.Row, actor: str) -> None:
    """The first time a claimed task's PR is recorded merged, tell its holder (a steer
    on the attempt it holds) and the origin conversation. Once per merge: a repeat
    observation records no change. Merged is not acceptance; nothing else moves."""
    # The holder takes no steers, or one just went out: the record stands.
    with contextlib.suppress(ConflictError, RateLimitedError):
        await steers.create(tx, row["id"], MERGE_STEER, actor)
    if row["origin_conversation_id"]:
        # Imported here: conversations import the orchestrator, which imports us.
        from argus_api.conversations import service as conversations

        await conversations.queue_task_update(tx, row["origin_conversation_id"])


async def update_continuation(
    tx: Tx, task_id: str, body: ContinuationUpdate, actor: str
) -> Task:
    """An orchestrator/admin settles facts the runtime cannot: the attempt's phase,
    known artifacts, preserved WIP, checklist items and evidence, environment status,
    or a decision to retire. Fenced by `expected_revision`; audited by event."""
    row = await _row(tx.conn, task_id)
    have = cont.load(row)
    cont.fence(have, body.expected_revision)
    at = tx.now
    artifacts = have.artifacts
    for a in body.artifacts:
        artifacts = cont.upsert_artifact(
            artifacts, _artifact(a, ContinuationProvenance.ORCHESTRATOR, at, None)
        )
    items = merge_acceptance(have.checklist, json.loads(row["acceptance"]))
    by_id = {i.id: i for i in items}
    for it in body.checklist:
        if it.id is None:
            if not it.text:
                raise ValidationFailedError("a new checklist item needs text")
            new_id = "or-" + _digest(it.text)[:10]
            by_id[new_id] = ChecklistItem(
                id=new_id,
                text=clean(it.text, 1000) or "",
                source="orchestrator",
                provenance=ContinuationProvenance.ORCHESTRATOR,
                status=it.status or ItemStatus.PENDING,
                evidence=clean(it.evidence, 2000),
                updated_at=at,
            )
        elif it.id in by_id:
            cur = by_id[it.id]
            by_id[it.id] = cur.model_copy(
                update={
                    "status": it.status or cur.status,
                    "evidence": clean(it.evidence, 2000)
                    if it.evidence is not None
                    else cur.evidence,
                    "provenance": ContinuationProvenance.ORCHESTRATOR,
                    "updated_at": at,
                }
            )
        else:
            raise ValidationFailedError(f"unknown checklist item {it.id}")
    wip = have.wip
    if body.clear_wip:
        wip = None
    if body.wip is not None:
        if (body.wip.ref or "").startswith(cont.INPUT_REF_PREFIX):
            await cont.check_input_ref(tx, row, body.wip.ref or "")
        wip = ContinuationWip(
            summary=clean(body.wip.summary, 1000) or "",
            ref=clean(body.wip.ref, 250),
            provenance=ContinuationProvenance.ORCHESTRATOR,
            observed_at=at,
        )
    known = {d.decision_id for d in have.decisions}
    if unknown := sorted(set(body.supersede_decisions) - known):
        raise ValidationFailedError(f"unknown decisions: {', '.join(unknown)}")
    decisions = [
        d.model_copy(update={"status": "superseded"})
        if d.decision_id in body.supersede_decisions
        else d
        for d in have.decisions
    ]
    want = have.model_copy(
        update={
            "phase": None if body.clear_phase else (body.phase or have.phase),
            "objective": clean(body.objective, 1000)
            if body.objective is not None
            else have.objective,
            "artifacts": artifacts,
            "wip": wip,
            "checklist": list(by_id.values()),
            "environment": cont.apply_environment(
                have.environment,
                [
                    EnvCheck(
                        **e.model_dump(), provenance=ContinuationProvenance.ORCHESTRATOR
                    )
                    for e in body.environment
                ],
                at,
            ),
            "decisions": decisions,
            "reconciliation": [] if body.clear_reconciliation else have.reconciliation,
        }
    )
    await cont.store(
        tx, task_id, have, want, actor, clean(body.reason, 300) or "update"
    )
    return await get_task(tx.conn, task_id)
