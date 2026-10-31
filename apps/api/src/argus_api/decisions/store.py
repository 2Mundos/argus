"""Low-level decision rows. No task logic here, so the task service can use it."""

import json
from uuid import uuid4

import aiosqlite
from argus_core.decisions import Audience, Decision, DecisionKind

from argus_api.db import Tx, ts
from argus_api.errors import NotFoundError
from argus_api.events import record

# Which decision kinds wait on a task being in a given state.
WAITS_IN = {
    "blocked": (DecisionKind.QUESTION,),
    "awaiting_approval": (DecisionKind.APPROVAL, DecisionKind.POLICY),
    "in_review": (DecisionKind.REVIEW,),
}


async def open_decision(
    tx: Tx,
    *,
    subject_type: str,
    subject_id: str,
    kind: DecisionKind,
    question: str,
    options: list[str] | None,
    audience: Audience,
    requested_by: str,
    payload: dict | None = None,
) -> str:
    decision_id = str(uuid4())
    await tx.conn.execute(
        "INSERT INTO decisions (id, subject_type, subject_id, kind, question, options,"
        " audience, state, requested_by, created_at, payload)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
        (
            decision_id,
            subject_type,
            subject_id,
            kind,
            question,
            json.dumps(options) if options is not None else None,
            audience,
            requested_by,
            ts(tx.now),
            json.dumps(payload) if payload is not None else None,
        ),
    )
    await record(
        tx,
        requested_by,
        "decision",
        decision_id,
        "decision.opened",
        {
            "kind": kind,
            "audience": audience,
            "subject_type": subject_type,
            "subject_id": subject_id,
        },
    )
    # Lazy: conversations imports settings, which imports knowledge, which imports
    # this module. A top-level import would cycle.
    from argus_api.conversations.service import queue_inbox_wake

    await queue_inbox_wake(
        tx, decision_id=decision_id, kind=str(kind), audience=str(audience)
    )
    return decision_id


async def cancel_open(
    tx: Tx,
    subject_type: str,
    subject_id: str,
    kinds: tuple[DecisionKind, ...] | None = None,
) -> list[str]:
    """Cancel open decisions that became moot; `kinds=None` means all of them."""
    sql = "SELECT id FROM decisions WHERE state = 'open' AND subject_type = ? AND subject_id = ?"
    params: list[str] = [subject_type, subject_id]
    if kinds is not None:
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        params += list(kinds)
    async with tx.conn.execute(sql, params) as cur:
        ids = [r["id"] for r in await cur.fetchall()]
    for decision_id in ids:
        await tx.conn.execute(
            "UPDATE decisions SET state = 'cancelled', decided_at = ?,"
            " decided_by = 'system' WHERE id = ?",
            (ts(tx.now), decision_id),
        )
        await record(tx, "system", "decision", decision_id, "decision.cancelled")
    return ids


async def get(conn: aiosqlite.Connection, decision_id: str) -> Decision:
    async with conn.execute(SELECT + " WHERE d.id = ?", (decision_id,)) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"decision not found: {decision_id}")
    return to_decision(row)


SELECT = (
    "SELECT d.*, coalesce(t.title, kp.title) AS subject_title,"
    " coalesce(t.project, kp.project) AS project FROM decisions d"
    " LEFT JOIN tasks t ON d.subject_type = 'task' AND t.id = d.subject_id"
    " LEFT JOIN knowledge_proposals kp"
    "  ON d.subject_type = 'knowledge_proposal' AND kp.id = d.subject_id"
)


def to_decision(row: aiosqlite.Row) -> Decision:
    data = dict(row)
    if data["options"] is not None:
        data["options"] = json.loads(data["options"])
    data["payload"] = json.loads(data["payload"]) if data.get("payload") else None
    return Decision.model_validate(data)
