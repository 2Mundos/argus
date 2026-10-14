"""Event history and the live stream for the dashboard (single-node 3.3)."""

import asyncio
import json
from collections.abc import AsyncGenerator, Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from argus_api.db import Database
from argus_api.deps import CallerDep, DbDep
from argus_api.events import Event, OverflowedError, to_sse

router = APIRouter(prefix="/events", tags=["events"])

KEEPALIVE_S = 15.0
BACKLOG_LIMIT = 1000


class EventOut(BaseModel):
    id: int
    at: datetime
    actor: str
    subject_type: str
    subject_id: str
    kind: str
    data: dict[str, Any]


@router.get("")
async def list_events(
    db: DbDep,
    _: CallerDep,
    subject_type: str | None = None,
    subject_id: str | None = None,
    before: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[EventOut]:
    """Event history, newest first. Page backwards with `before`."""
    sql, params = "SELECT * FROM events WHERE 1 = 1", []
    for column, value in (("subject_type", subject_type), ("subject_id", subject_id)):
        if value is not None:
            sql += f" AND {column} = ?"
            params.append(value)
    if before is not None:
        sql += " AND id < ?"
        params.append(before)
    async with (
        db.read() as conn,
        conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*params, limit)) as cur,
    ):
        rows = await cur.fetchall()
    return [
        EventOut.model_validate({**dict(r), "data": json.loads(r["data"])})
        for r in rows
    ]


@router.get("/stream", response_class=StreamingResponse)
async def stream_events(
    db: DbDep,
    _: CallerDep,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Server-sent events. Reconnect with `Last-Event-ID` to resume without gaps."""
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else None
    return StreamingResponse(
        event_stream(db, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def event_stream(
    db: Database, after: int | None, keepalive: float = KEEPALIVE_S
) -> AsyncGenerator[str]:
    """Replay committed events after `after` from the table, then go live.

    Subscribing happens before the replay, so an event committed in between is
    seen either in the replay or live; ids dedupe the overlap. A subscriber that
    overflows ends the stream; the client reconnects with Last-Event-ID.
    """
    with db.bus.subscribe() as sub:
        if after is None:
            async with (
                db.read() as conn,
                conn.execute("SELECT coalesce(max(id), 0) FROM events") as cur,
            ):
                row = await cur.fetchone()
            cursor = row[0] if row else 0
        else:
            cursor = after
            while True:  # replay in batches until caught up
                async with (
                    db.read() as conn,
                    conn.execute(
                        "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?",
                        (cursor, BACKLOG_LIMIT),
                    ) as cur,
                ):
                    rows = list(await cur.fetchall())
                for r in rows:
                    event = Event(
                        r["id"],
                        datetime.fromisoformat(r["at"]),
                        r["actor"],
                        r["subject_type"],
                        r["subject_id"],
                        r["kind"],
                        json.loads(r["data"]),
                    )
                    yield to_sse(event)
                    cursor = event.id
                if len(rows) < BACKLOG_LIMIT:
                    break
        while True:
            try:
                async with asyncio.timeout(keepalive):
                    event = await sub.next_matching(_after(cursor))
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            except OverflowedError:
                return
            yield to_sse(event)
            cursor = event.id


def _after(cursor: int) -> Callable[[Event], bool]:
    return lambda e: e.id > cursor
