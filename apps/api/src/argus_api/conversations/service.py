"""Conversations and the argus chat transport
(docs/specs/internal-orchestrator.spec.md 3.2-3.7)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import aiosqlite
from argus_core.conversations import (
    AgentChatReply,
    Conversation,
    ConversationCreate,
    ConversationKind,
    ConversationPatch,
    Message,
    MessageCreate,
    TitleSource,
    TurnUsage,
)
from argus_core.redact import redact, redact_private_keys

from argus_api.db import Tx, ts
from argus_api.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationFailedError,
)
from argus_api.events import record
from argus_api.settings import service as settings_service

HUMAN_MAX = 8000
ORCH_MAX = 32 * 1024
MAX_QUEUED = 20
RATE_LIMIT = timedelta(seconds=1)
# A delivered turn with no reply after this long is given up on, so one crashed turn
# cannot block the channel.
TURN_TIMEOUT = timedelta(minutes=15)
MAX_TURNS_KEY = "orchestrator.max_concurrent_turns"
BUDGET_KEY = "orchestrator.daily_budget_usd"

# Active turn: a human message delivered but not yet answered.
_TURN_OPEN = ("delivered",)


class RateLimitedError(DomainError):
    status = 429
    code = "rate_limited"


_TRAILING_SPACE = re.compile(r"[^\S\n]+$", re.MULTILINE)
_BLANK_RUNS = re.compile(r"\n{3,}")


def _clean_body(
    body: str, *, limit: int, secrets: tuple[str, ...] = ()
) -> tuple[str, bool]:
    # Bodies are Markdown: keep line breaks and indentation, trim the noise.
    text = _TRAILING_SPACE.sub("", body.strip().replace("\r\n", "\n"))
    text = _BLANK_RUNS.sub("\n\n", text)
    if not text:
        raise ValidationFailedError("body is empty after trimming")
    text = redact_private_keys(text)
    text = redact(text, secrets)
    truncated = len(text) > limit
    if truncated:
        text = text[:limit]
    return text, truncated


def _to_conversation(row: aiosqlite.Row, cost: float | None = None) -> Conversation:
    return Conversation(
        id=row["id"],
        kind=row["kind"],
        title=row["title"],
        title_source=row["title_source"],
        created_by=row["created_by"],
        archived_at=datetime.fromisoformat(row["archived_at"])
        if row["archived_at"]
        else None,
        session_ref=row["session_ref"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        cost_usd=cost,
    )


def _to_message(row: aiosqlite.Row) -> Message:
    return Message(
        id=row["id"],
        conversation_id=row["conversation_id"],
        author=row["author"],
        body=row["body"],
        state=row["state"],
        truncated=bool(row["truncated"]),
        turn_id=row["turn_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        delivered_at=datetime.fromisoformat(row["delivered_at"])
        if row["delivered_at"]
        else None,
    )


async def get_conversation(conn: aiosqlite.Connection, conv_id: str) -> Conversation:
    async with conn.execute(
        "SELECT * FROM conversations WHERE id = ?", (conv_id,)
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"conversation not found: {conv_id}")
    cost = await conversation_cost(conn, conv_id)
    return _to_conversation(row, cost)


async def conversation_cost(conn: aiosqlite.Connection, conv_id: str) -> float | None:
    async with conn.execute(
        "SELECT sum(cost_usd) FROM conversation_usage WHERE conversation_id = ?"
        " AND cost_usd IS NOT NULL",
        (conv_id,),
    ) as cur:
        row = await cur.fetchone()
    total = row[0] if row else None
    return float(total) if total is not None else None


async def list_conversations(
    conn: aiosqlite.Connection, *, include_archived: bool = False
) -> list[Conversation]:
    where = "" if include_archived else " WHERE archived_at IS NULL"
    async with conn.execute(
        f"SELECT * FROM conversations{where}"  # noqa: S608
        " ORDER BY CASE kind WHEN 'updates' THEN 0 ELSE 1 END, updated_at DESC, id"
    ) as cur:
        rows = await cur.fetchall()
    return [
        _to_conversation(row, await conversation_cost(conn, row["id"])) for row in rows
    ]


async def create_conversation(
    tx: Tx, body: ConversationCreate, actor: str
) -> Conversation:
    conv_id = str(uuid4())
    now = ts(tx.now)
    title = (body.title or "New chat").strip() or "New chat"
    source = TitleSource.ADMIN if body.title else TitleSource.ORCHESTRATOR
    # Untitled chats start with a placeholder; the first orchestrator reply may rename
    # when title_source is still orchestrator.
    if body.title is None:
        source = TitleSource.ORCHESTRATOR
    await tx.conn.execute(
        "INSERT INTO conversations (id, kind, title, title_source, created_by,"
        " archived_at, session_ref, created_at, updated_at)"
        " VALUES (?, 'chat', ?, ?, ?, NULL, NULL, ?, ?)",
        (conv_id, title, source, actor, now, now),
    )
    await record(
        tx,
        actor,
        "conversation",
        conv_id,
        "conversation.created",
        {"kind": "chat", "title": title},
    )
    return await get_conversation(tx.conn, conv_id)


async def patch_conversation(
    tx: Tx, conv_id: str, body: ConversationPatch, actor: str
) -> Conversation:
    conv = await get_conversation(tx.conn, conv_id)
    if conv.kind == ConversationKind.UPDATES:
        raise ConflictError(
            "the Updates conversation cannot be renamed", code="updates"
        )
    title = body.title.strip()
    await tx.conn.execute(
        "UPDATE conversations SET title = ?, title_source = 'admin',"
        " updated_at = ? WHERE id = ?",
        (title, ts(tx.now), conv_id),
    )
    await record(
        tx,
        actor,
        "conversation",
        conv_id,
        "conversation.updated",
        {"title": title, "title_source": "admin"},
    )
    return await get_conversation(tx.conn, conv_id)


async def archive_conversation(
    tx: Tx, conv_id: str, actor: str, *, archive: bool
) -> Conversation:
    conv = await get_conversation(tx.conn, conv_id)
    if conv.kind == ConversationKind.UPDATES:
        raise ConflictError(
            "the Updates conversation cannot be archived", code="updates"
        )
    value = ts(tx.now) if archive else None
    await tx.conn.execute(
        "UPDATE conversations SET archived_at = ?, updated_at = ? WHERE id = ?",
        (value, ts(tx.now), conv_id),
    )
    await record(
        tx,
        actor,
        "conversation",
        conv_id,
        "conversation.updated",
        {"archived": archive},
    )
    return await get_conversation(tx.conn, conv_id)


async def list_messages(
    conn: aiosqlite.Connection, conv_id: str, *, limit: int = 200
) -> list[Message]:
    await get_conversation(conn, conv_id)
    async with conn.execute(
        "SELECT * FROM messages WHERE conversation_id = ?"
        " ORDER BY created_at DESC, id DESC LIMIT ?",
        (conv_id, limit),
    ) as cur:
        rows = list(await cur.fetchall())
    rows.reverse()
    return [_to_message(r) for r in rows]


async def _queued_count(conn: aiosqlite.Connection, conv_id: str) -> int:
    async with conn.execute(
        "SELECT count(*) FROM messages WHERE conversation_id = ? AND state = 'queued'",
        (conv_id,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _last_human_at(conn: aiosqlite.Connection, conv_id: str) -> datetime | None:
    async with conn.execute(
        "SELECT created_at FROM messages WHERE conversation_id = ?"
        " AND author LIKE 'admin:%' ORDER BY created_at DESC LIMIT 1",
        (conv_id,),
    ) as cur:
        row = await cur.fetchone()
    return datetime.fromisoformat(row[0]) if row else None


async def post_human_message(
    tx: Tx,
    conv_id: str,
    body: MessageCreate,
    actor: str,
    *,
    secrets: tuple[str, ...] = (),
) -> Message:
    conv = await get_conversation(tx.conn, conv_id)
    if conv.archived_at is not None:
        raise ConflictError("conversation is archived", code="archived")
    if await _queued_count(tx.conn, conv_id) >= MAX_QUEUED:
        raise ConflictError(
            f"at most {MAX_QUEUED} queued messages per conversation",
            code="too_many_queued",
        )
    last = await _last_human_at(tx.conn, conv_id)
    if last is not None and tx.now - last < RATE_LIMIT:
        wait = RATE_LIMIT - (tx.now - last)
        raise RateLimitedError(
            f"one human message per second; wait {wait.total_seconds():.1f}s",
            details=[str(wait.total_seconds())],
        )
    text, truncated = _clean_body(body.body, limit=HUMAN_MAX, secrets=secrets)
    msg_id = str(uuid4())
    now = ts(tx.now)
    await tx.conn.execute(
        "INSERT INTO messages (id, conversation_id, author, body, state, truncated,"
        " turn_id, created_at, delivered_at) VALUES (?, ?, ?, ?, 'queued', ?, NULL, ?, NULL)",
        (msg_id, conv_id, actor, text, int(truncated), now),
    )
    await tx.conn.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id)
    )
    await record(
        tx,
        actor,
        "conversation",
        conv_id,
        "chat.message",
        {"message_id": msg_id, "author": actor, "state": "queued"},
    )
    async with tx.conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    return _to_message(row)


async def _next_claimable(
    conn: aiosqlite.Connection, now: datetime, path
) -> aiosqlite.Row | None:
    """Oldest queued human message that may start a turn now, or None.

    A conversation runs one turn at a time, at most `orchestrator.max_concurrent_turns`
    chat conversations run at once (Updates is extra), and a delivered turn older than
    `TURN_TIMEOUT` no longer blocks.
    """
    limit = int(
        (
            await settings_service.get_setting(
                conn, path, settings_service.GLOBAL, MAX_TURNS_KEY
            )
        ).value
    )
    live = ts(now - TURN_TIMEOUT)
    async with conn.execute(
        "SELECT m.* FROM messages m"
        " JOIN conversations c ON c.id = m.conversation_id"
        " WHERE m.state = 'queued' AND c.archived_at IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM messages d WHERE d.state = 'delivered'"
        "   AND d.conversation_id = m.conversation_id AND d.delivered_at > ?)"
        " AND (c.kind = 'updates' OR ? > (SELECT count(DISTINCT d.conversation_id)"
        "   FROM messages d JOIN conversations dc ON dc.id = d.conversation_id"
        "   WHERE d.state = 'delivered' AND d.delivered_at > ? AND dc.kind = 'chat'))"
        " ORDER BY m.created_at, m.id LIMIT 1",
        (live, limit, live),
    ) as cur:
        return await cur.fetchone()


async def has_claimable(conn: aiosqlite.Connection, now: datetime, path) -> bool:
    """Read-only pre-check, so idle polls never take the writer."""
    if await _next_claimable(conn, now, path) is None:
        return False
    if await daily_cap_reached(conn, now, path):
        # Capped: only worth the writer to post today's notice (once per UTC day).
        return await _cap_notice_target(conn, now) is not None
    return True


async def _expire_stale_turns(tx: Tx) -> None:
    async with tx.conn.execute(
        "SELECT id, conversation_id FROM messages"
        " WHERE state = 'delivered' AND delivered_at <= ?",
        (ts(tx.now - TURN_TIMEOUT),),
    ) as cur:
        stale = list(await cur.fetchall())
    for row in stale:
        await tx.conn.execute(
            "UPDATE messages SET state = 'answered' WHERE id = ?", (row["id"],)
        )
        await _system_message(
            tx, row["conversation_id"], "The orchestrator did not answer this turn."
        )


async def _system_message(tx: Tx, conv_id: str, body: str) -> None:
    now = ts(tx.now)
    await tx.conn.execute(
        "INSERT INTO messages (id, conversation_id, author, body, state, truncated,"
        " turn_id, created_at, delivered_at) VALUES (?, ?, 'system', ?, 'posted', 0,"
        " NULL, ?, NULL)",
        (str(uuid4()), conv_id, body, now),
    )
    await tx.conn.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id)
    )


async def _cap_notice_target(conn: aiosqlite.Connection, now: datetime) -> str | None:
    """Updates conversation id when today's cap notice is still to be posted."""
    async with conn.execute(
        "SELECT id FROM conversations WHERE kind = 'updates'"
    ) as cur:
        updates = await cur.fetchone()
    if updates is None:
        return None
    async with conn.execute(
        "SELECT 1 FROM messages WHERE conversation_id = ? AND author = 'system'"
        " AND body LIKE 'Daily budget reached%' AND created_at >= ? LIMIT 1",
        (updates["id"], now.strftime("%Y-%m-%dT00:00:00+00:00")),
    ) as cur:
        posted = await cur.fetchone() is not None
    return None if posted else updates["id"]


async def claim_next(
    tx: Tx, *, agent_id: str, path
) -> tuple[Conversation, Message] | None:
    """Claim the oldest deliverable queued human message.

    Delivery is idempotent by message id: a second claim of a delivered id is a no-op
    at the channel (it only sees queued rows here).
    """
    # Daily cap: stop delivering when spend since 00:00 UTC reaches the budget.
    if await daily_cap_reached(tx.conn, tx.now, path):
        target = await _cap_notice_target(tx.conn, tx.now)
        if target is not None and await _next_claimable(tx.conn, tx.now, path):
            note = "Daily budget reached; messages stay queued until the next UTC day."
            if (await daily_spend_usd(tx.conn, tx.now))[1]:
                note += (
                    " Some of today's usage could not be priced, so spend is a floor."
                )
            await _system_message(tx, target, note)
        return None
    await _expire_stale_turns(tx)
    row = await _next_claimable(tx.conn, tx.now, path)
    if row is None:
        return None
    turn_id = str(uuid4())
    now = ts(tx.now)
    await tx.conn.execute(
        "UPDATE messages SET state = 'delivered', delivered_at = ?, turn_id = ?"
        " WHERE id = ? AND state = 'queued'",
        (now, turn_id, row["id"]),
    )
    await record(
        tx,
        f"orchestrator:{agent_id}",
        "conversation",
        row["conversation_id"],
        "chat.turn",
        {"phase": "started", "turn_id": turn_id, "message_id": row["id"]},
    )
    conv = await get_conversation(tx.conn, row["conversation_id"])
    async with tx.conn.execute(
        "SELECT * FROM messages WHERE id = ?", (row["id"],)
    ) as cur:
        claimed = await cur.fetchone()
    assert claimed is not None
    return conv, _to_message(claimed)


async def post_reply(
    tx: Tx,
    conv_id: str,
    body: AgentChatReply,
    agent_id: str,
    *,
    secrets: tuple[str, ...] = (),
    pricing_file: Path | None = None,
) -> Message:
    await get_conversation(tx.conn, conv_id)
    text, truncated = _clean_body(body.body, limit=ORCH_MAX, secrets=secrets)
    author = f"orchestrator:{agent_id}"
    msg_id = str(uuid4())
    now = ts(tx.now)
    turn_id = body.turn_id
    await tx.conn.execute(
        "INSERT INTO messages (id, conversation_id, author, body, state, truncated,"
        " turn_id, created_at, delivered_at) VALUES (?, ?, ?, ?, 'posted', ?, ?, ?, NULL)",
        (msg_id, conv_id, author, text, int(truncated), turn_id, now),
    )
    # Mark human messages of this turn answered.
    if turn_id:
        await tx.conn.execute(
            "UPDATE messages SET state = 'answered' WHERE conversation_id = ?"
            " AND turn_id = ? AND state = 'delivered'",
            (conv_id, turn_id),
        )
    else:
        await tx.conn.execute(
            "UPDATE messages SET state = 'answered' WHERE conversation_id = ?"
            " AND state = 'delivered'",
            (conv_id,),
        )
    # Title proposal from the first orchestrator reply.
    async with tx.conn.execute(
        "SELECT title_source FROM conversations WHERE id = ?", (conv_id,)
    ) as cur:
        title_row = await cur.fetchone()
    assert title_row is not None
    src = title_row["title_source"]
    if src == TitleSource.ORCHESTRATOR and body.title:
        title = body.title.strip()[:200]
        await tx.conn.execute(
            "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
            (title, now, conv_id),
        )
    else:
        await tx.conn.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id)
        )
    if body.usage is not None:
        await record_usage(tx, conv_id, turn_id, body.usage, pricing_file)
    await record(
        tx,
        author,
        "conversation",
        conv_id,
        "chat.message",
        {"message_id": msg_id, "author": author, "state": "posted"},
    )
    if turn_id:
        await record(
            tx,
            author,
            "conversation",
            conv_id,
            "chat.turn",
            {"phase": "finished", "turn_id": turn_id, "message_id": msg_id},
        )
    async with tx.conn.execute("SELECT * FROM messages WHERE id = ?", (msg_id,)) as cur:
        out = await cur.fetchone()
    assert out is not None
    return _to_message(out)


async def record_usage(
    tx: Tx,
    conv_id: str,
    turn_id: str | None,
    usage: TurnUsage,
    pricing_file: Path | None,
) -> None:
    """Price a turn with the same price table as the cost view."""
    from argus_api.accounts.pricing import load as load_prices

    cost_usd = None
    measurement = "unknown"
    if usage.model and (
        usage.input_tokens is not None or usage.output_tokens is not None
    ):
        priced = load_prices(pricing_file).cost(
            usage.model, usage.input_tokens, usage.output_tokens, None
        )
        if priced is None:
            measurement = "unpriced"
        else:
            cost_usd = priced
            measurement = "measured"
    await tx.conn.execute(
        "INSERT INTO conversation_usage (conversation_id, turn_id, input_tokens,"
        " output_tokens, cost_usd, model, at, measurement)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            conv_id,
            turn_id,
            usage.input_tokens,
            usage.output_tokens,
            cost_usd,
            usage.model,
            ts(tx.now),
            measurement,
        ),
    )


async def daily_spend_usd(
    conn: aiosqlite.Connection, now: datetime
) -> tuple[float, bool]:
    """Spend since 00:00 UTC and whether any unpriced/unknown rows exist today."""
    day_start = now.strftime("%Y-%m-%dT00:00:00+00:00")
    async with conn.execute(
        "SELECT coalesce(sum(cost_usd), 0),"
        " coalesce(max(measurement IN ('unpriced', 'unknown')), 0)"
        " FROM conversation_usage WHERE at >= ?",
        (day_start,),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    return float(row[0]), bool(row[1])


async def daily_cap_reached(conn: aiosqlite.Connection, now: datetime, path) -> bool:
    budget = (
        await settings_service.get_setting(
            conn, path, settings_service.GLOBAL, BUDGET_KEY
        )
    ).value
    spend, _ = await daily_spend_usd(conn, now)
    return spend >= float(budget)


async def set_session_ref(tx: Tx, conv_id: str, session_ref: str | None) -> None:
    await get_conversation(tx.conn, conv_id)
    await tx.conn.execute(
        "UPDATE conversations SET session_ref = ?, updated_at = ? WHERE id = ?",
        (session_ref, ts(tx.now), conv_id),
    )


async def note_session_restored(tx: Tx, conv_id: str) -> None:
    await get_conversation(tx.conn, conv_id)
    await _system_message(tx, conv_id, "Session restored from history.")


async def chat_context(
    conn: aiosqlite.Connection, conv_id: str, *, message_limit: int = 20
) -> tuple[list[Message], list[tuple[str, str, str]]]:
    """Last N messages and tasks with this conversation as origin (3.4 seed)."""
    messages = await list_messages(conn, conv_id, limit=message_limit)
    async with conn.execute(
        "SELECT id, title, state FROM tasks WHERE origin_conversation_id = ?"
        " AND archived_at IS NULL ORDER BY created_at DESC, id DESC LIMIT 50",
        (conv_id,),
    ) as cur:
        tasks = [(r["id"], r["title"], r["state"]) for r in await cur.fetchall()]
    return messages, tasks
