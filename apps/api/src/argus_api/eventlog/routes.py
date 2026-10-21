"""Event history and the live stream for the dashboard (single-node 3.3)."""

import asyncio
import json
from collections.abc import AsyncGenerator, Callable, Collection
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from argus_api.access import (
    has_leased_task,
    is_global_reader,
    leased_projects,
    worker_event_filter,
)
from argus_api.auth import Identity
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
    caller: CallerDep,
    subject_type: str | None = None,
    subject_id: str | None = None,
    before: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[EventOut]:
    """Event history, newest first. Page backwards with `before`. Workers only
    see events for projects of tasks they have leased."""
    sql, params = "SELECT * FROM events WHERE 1 = 1", []
    async with db.read() as conn:
        scope = await _scope(conn, caller)
        if scope is not None:
            clause, scope_params = scope
            sql += f" AND ({clause})"
            params += scope_params
        for column, value in (
            ("subject_type", subject_type),
            ("subject_id", subject_id),
        ):
            if value is not None:
                sql += f" AND {column} = ?"
                params.append(value)
        if before is not None:
            sql += " AND id < ?"
            params.append(before)
        async with conn.execute(
            sql + " ORDER BY id DESC LIMIT ?", (*params, limit)
        ) as cur:
            rows = await cur.fetchall()
    return [
        EventOut.model_validate({**dict(r), "data": json.loads(r["data"])})
        for r in rows
    ]


WAIT_LIMIT = 100


class EventWait(BaseModel):
    events: list[EventOut]  # oldest first, at most 100
    next_cursor: int  # pass as `after` next time; unchanged when nothing came
    timed_out: bool  # nothing matched within wait_s


@router.get("/wait")
async def wait_events(
    db: DbDep,
    caller: CallerDep,
    after: Annotated[int | None, Query(ge=0)] = None,
    wait_s: Annotated[float, Query(ge=0, le=30)] = 0,
    subject_type: str | None = None,
    subject_id: str | None = None,
) -> EventWait:
    """Events after the cursor `after`, oldest first, waiting up to `wait_s`
    seconds for one when none has happened yet (long-poll, like a claim). Without
    `after`, waits for the next event from now. Keep `next_cursor` for the next
    call; nothing is missed between calls."""
    async with db.read() as conn:
        scope = await _scope(conn, caller)
    return await wait(db, after, wait_s, subject_type, subject_id, scope=scope)


async def wait(
    db: Database,
    after: int | None,
    wait_s: float,
    subject_type: str | None = None,
    subject_id: str | None = None,
    *,
    scope: tuple[str, list[str]] | None = None,
) -> EventWait:
    def matches(e: Event) -> bool:
        return (subject_type is None or e.subject_type == subject_type) and (
            subject_id is None or e.subject_id == subject_id
        )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    # Subscribe before reading, so an event committed in between is not missed.
    with db.bus.subscribe() as sub:
        if after is None:
            async with (
                db.read() as conn,
                conn.execute("SELECT coalesce(max(id), 0) FROM events") as cur,
            ):
                row = await cur.fetchone()
            after = row[0] if row else 0
        while True:
            events = await _events_after(
                db, after, subject_type, subject_id, scope=scope
            )
            if events:
                return EventWait(
                    events=events, next_cursor=events[-1].id, timed_out=False
                )
            remaining = deadline - loop.time()
            if remaining <= 0:
                return EventWait(events=[], next_cursor=after, timed_out=True)
            try:
                async with asyncio.timeout(remaining):
                    await sub.next_matching(lambda e: e.id > after and matches(e))
            except OverflowedError:
                continue  # missed events on the bus: the table has them
            except TimeoutError:
                return EventWait(events=[], next_cursor=after, timed_out=True)


async def _events_after(
    db: Database,
    after: int,
    subject_type: str | None,
    subject_id: str | None,
    *,
    scope: tuple[str, list[str]] | None = None,
) -> list[EventOut]:
    sql = "SELECT * FROM events WHERE id > ?"
    params: list[int | str] = [after]
    if scope is not None:
        clause, scope_params = scope
        sql += f" AND ({clause})"
        params += scope_params
    for column, value in (("subject_type", subject_type), ("subject_id", subject_id)):
        if value is not None:
            sql += f" AND {column} = ?"
            params.append(value)
    async with (
        db.read() as conn,
        conn.execute(sql + " ORDER BY id LIMIT ?", (*params, WAIT_LIMIT)) as cur,
    ):
        rows = await cur.fetchall()
    return [
        EventOut.model_validate({**dict(r), "data": json.loads(r["data"])})
        for r in rows
    ]


@router.get("/stream", response_class=StreamingResponse)
async def stream_events(
    db: DbDep,
    caller: CallerDep,
    last_event_id: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    """Server-sent events. Reconnect with `Last-Event-ID` to resume without gaps."""
    after = int(last_event_id) if last_event_id and last_event_id.isdigit() else None
    async with db.read() as conn:
        scope = await _scope(conn, caller)
        projects = (
            None
            if scope is None
            else (
                await leased_projects(conn, caller.agent_id)
                if caller.agent_id
                else set()
            )
        )
    return StreamingResponse(
        event_stream(
            db, after, scope=scope, projects=projects, agent_id=caller.agent_id
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def event_stream(
    db: Database,
    after: int | None,
    keepalive: float = KEEPALIVE_S,
    *,
    scope: tuple[str, list[str]] | None = None,
    projects: Collection[str] | None = None,
    agent_id: str | None = None,
) -> AsyncGenerator[str]:
    """Replay committed events after `after` from the table, then go live.

    Subscribing happens before the replay, so an event committed in between is
    seen either in the replay or live; ids dedupe the overlap. A subscriber that
    overflows ends the stream; the client reconnects with Last-Event-ID.
    Workers only receive events for leased tasks and their leased projects (live
    filter uses the project set captured at subscribe time; task grants are
    checked per event).
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
                sql = "SELECT * FROM events WHERE id > ?"
                params: list[Any] = [cursor]
                if scope is not None:
                    clause, scope_params = scope
                    sql += f" AND ({clause})"
                    params += scope_params
                async with (
                    db.read() as conn,
                    conn.execute(
                        sql + " ORDER BY id LIMIT ?",
                        (*params, BACKLOG_LIMIT),
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
            if projects is not None and not await _live_allowed(
                db, event, projects, agent_id
            ):
                cursor = event.id
                continue
            yield to_sse(event)
            cursor = event.id


def _after(cursor: int) -> Callable[[Event], bool]:
    return lambda e: e.id > cursor


async def _scope(conn, caller: Identity) -> tuple[str, list[str]] | None:
    if is_global_reader(caller):
        return None
    assert caller.agent_id is not None
    projects = await leased_projects(conn, caller.agent_id)
    return worker_event_filter(caller, projects)


async def _live_allowed(
    db: Database, event: Event, projects: Collection[str], agent_id: str | None
) -> bool:
    """Whether a live bus event is readable by the worker; same rule as
    worker_event_filter."""
    if agent_id is None:
        return False
    if event.subject_type == "task":
        async with db.read() as conn:
            return await has_leased_task(conn, agent_id, event.subject_id)
    if not projects:
        return False
    if event.subject_type == "project":
        return event.subject_id in projects
    if event.subject_type == "knowledge_page":
        async with (
            db.read() as conn,
            conn.execute(
                "SELECT project FROM knowledge_pages WHERE id = ?", (event.subject_id,)
            ) as cur,
        ):
            row = await cur.fetchone()
        return row is not None and row[0] in projects
    if event.subject_type == "decision":
        async with (
            db.read() as conn,
            conn.execute(
                "SELECT d.subject_type, d.subject_id, kp.project FROM decisions d"
                " LEFT JOIN knowledge_proposals kp"
                "  ON d.subject_type = 'knowledge_proposal' AND kp.id = d.subject_id"
                " WHERE d.id = ?",
                (event.subject_id,),
            ) as cur,
        ):
            row = await cur.fetchone()
        if row is None:
            return False
        if row["subject_type"] == "task":
            async with db.read() as conn:
                return await has_leased_task(conn, agent_id, row["subject_id"])
        return row["project"] in projects
    return False
