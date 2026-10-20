"""Decision lists and their projections (MCP operations 3.1, 3.2).

`summary` names the decision, its subject and options, never the question or
payload bodies; `detail` carries them, each long text cut to TEXT_CAP.
"""

from datetime import datetime
from typing import Any

import aiosqlite
from argus_core.decisions import Audience, Decision, DecisionKind, DecisionState
from pydantic import BaseModel

from argus_api.db import Snapshot
from argus_api.decisions import store
from argus_api.paging import (
    PageInfo,
    View,
    cap,
    clamp,
    decode_cursor,
    encode_cursor,
)


class DecisionSummary(BaseModel):
    id: str
    subject_type: str
    subject_id: str
    subject_title: str | None
    project: str | None
    kind: DecisionKind
    audience: Audience
    state: DecisionState
    options: list[str] | None
    answer_choice: str | None
    requested_by: str
    decided_by: str | None
    created_at: datetime
    decided_at: datetime | None
    age_s: int  # since it was opened
    truncated: dict[str, int] = {}  # field -> full length in bytes


class DecisionDetail(Decision, DecisionSummary):
    pass


class DecisionPage(PageInfo):
    items: list[DecisionSummary | DecisionDetail]


SUMMARY_OMITS = sorted(set(Decision.model_fields) - set(DecisionSummary.model_fields))


async def list_decisions(
    snap: Snapshot,
    *,
    state: DecisionState | None = DecisionState.OPEN,
    subject_id: str | None = None,
    project: str | None = None,
    kinds: list[DecisionKind] | None = None,
    view: View = "summary",
    limit: int = 25,
    cursor: str | None = None,
) -> DecisionPage:
    """Oldest first, keyset-paged on (created_at, id)."""
    limit = clamp(limit)
    filters: dict[str, Any] = {"state": state, "subject_id": subject_id}
    filters |= {"project": project, "kinds": sorted(kinds or [])}
    where, params = ["1 = 1"], []
    if state is not None:
        where.append("d.state = ?")
        params.append(state)
    if subject_id is not None:
        where.append("d.subject_id = ?")
        params.append(subject_id)
    if project is not None:
        where.append("coalesce(t.project, kp.project) = ?")
        params.append(project)
    if kinds:
        where.append(f"d.kind IN ({','.join('?' * len(kinds))})")
        params += kinds
    sql = store.SELECT + " WHERE " + " AND ".join(where)
    async with snap.conn.execute(f"SELECT count(*) FROM ({sql})", params) as cur:  # noqa: S608
        row = await cur.fetchone()
    assert row is not None
    total = row[0]
    if (after := decode_cursor(cursor, filters)) is not None:
        sql += " AND (d.created_at, d.id) > (?, ?)"
        params += after
    async with snap.conn.execute(
        sql + " ORDER BY d.created_at, d.id LIMIT ?", [*params, limit + 1]
    ) as cur:
        rows = list(await cur.fetchall())
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [project_row(snap.now, r, view) for r in rows]
    omitted = {f for i in items for f in i.truncated}
    if view == "summary":
        omitted |= set(SUMMARY_OMITS)
    last = rows[-1] if has_more else None
    return DecisionPage(
        items=items,
        total=total,
        has_more=has_more,
        next_cursor=encode_cursor([last["created_at"], last["id"]], filters)
        if last is not None
        else None,
        omitted=sorted(omitted),
    )


def project_row(
    now: datetime, row: aiosqlite.Row, view: View
) -> DecisionSummary | DecisionDetail:
    decision = store.to_decision(row)
    age = max(0, int((now - decision.created_at).total_seconds()))
    if view == "summary":
        return DecisionSummary.model_validate({**decision.model_dump(), "age_s": age})
    truncated: dict[str, int] = {}
    data = decision.model_dump()
    data["question"] = cap(decision.question, "question", truncated)
    data["answer_text"] = cap(decision.answer_text, "answer_text", truncated)
    return DecisionDetail.model_validate({**data, "age_s": age, "truncated": truncated})
