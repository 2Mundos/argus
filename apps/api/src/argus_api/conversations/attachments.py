"""Chat attachments: bytes in the local artifact store, rows in message_attachments
(docs/specs/internal-orchestrator.spec.md 3.2)."""

from __future__ import annotations

import codecs
import re
from base64 import b64encode
from collections.abc import AsyncIterator
from datetime import datetime
from uuid import uuid4

import aiosqlite
import anyio
from argus_core.conversations import ChatAttachment, ChatAttachmentText

from argus_api.artifacts.service import check_media_type, is_text
from argus_api.artifacts.store import LocalArtifactStore
from argus_api.db import Database, Tx, ts
from argus_api.errors import (
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)
from argus_api.events import record

MAX_PER_MESSAGE = 10
MAX_PER_CONVERSATION = 50

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def store_key(conversation_id: str, attachment_id: str) -> str:
    return f"conversations/{conversation_id}/{attachment_id}"


def check_filename(name: str) -> str:
    name = _CONTROL.sub("", name.replace("\\", "/").rsplit("/", 1)[-1]).strip()
    if not name or name in {".", ".."} or len(name) > 200:
        raise ValidationFailedError(
            "filename must be 1-200 characters without a path",
            code="invalid_filename",
        )
    return name


def _to_attachment(row: aiosqlite.Row) -> ChatAttachment:
    return ChatAttachment(
        id=row["id"],
        conversation_id=row["conversation_id"],
        message_id=row["message_id"],
        filename=row["filename"],
        media_type=row["media_type"],
        size=int(row["size"]),
        sha256=row["sha256"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


async def get_attachment(
    conn: aiosqlite.Connection, conversation_id: str, attachment_id: str
) -> ChatAttachment:
    async with conn.execute(
        "SELECT * FROM message_attachments WHERE id = ? AND conversation_id = ?",
        (attachment_id, conversation_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        raise NotFoundError(f"attachment not found: {attachment_id}")
    return _to_attachment(row)


async def attachments_for(
    conn: aiosqlite.Connection, conversation_id: str, message_ids: list[str]
) -> dict[str, list[ChatAttachment]]:
    found: dict[str, list[ChatAttachment]] = {i: [] for i in message_ids}
    if not message_ids:
        return found
    wanted = set(message_ids)
    async with conn.execute(
        "SELECT * FROM message_attachments WHERE conversation_id = ?"
        " AND message_id IS NOT NULL ORDER BY created_at, id",
        (conversation_id,),
    ) as cur:
        rows = await cur.fetchall()
    for row in rows:
        if row["message_id"] in wanted:
            found[row["message_id"]].append(_to_attachment(row))
    return found


async def attach(
    db: Database,
    store: LocalArtifactStore,
    *,
    conversation_id: str,
    filename: str,
    media_type: str,
    chunks: AsyncIterator[bytes],
    actor: str,
) -> ChatAttachment:
    filename = check_filename(filename)
    media_type = check_media_type(media_type or "application/octet-stream")
    staged = await store.stage(chunks)
    promoted = None
    try:
        async with db.write() as tx:
            async with tx.conn.execute(
                "SELECT archived_at FROM conversations WHERE id = ?",
                (conversation_id,),
            ) as cur:
                conv = await cur.fetchone()
            if conv is None:
                raise NotFoundError(f"conversation not found: {conversation_id}")
            if conv["archived_at"] is not None:
                raise ConflictError("conversation is archived", code="archived")
            async with tx.conn.execute(
                "SELECT count(*) FROM message_attachments WHERE conversation_id = ?",
                (conversation_id,),
            ) as cur:
                row = await cur.fetchone()
            assert row is not None
            if row[0] >= MAX_PER_CONVERSATION:
                raise ConflictError(
                    f"conversation already has {row[0]} attachments"
                    f" (limit {MAX_PER_CONVERSATION})",
                    code="attachment_limit",
                )
            attachment_id = str(uuid4())
            await tx.conn.execute(
                "INSERT INTO message_attachments (id, conversation_id, message_id,"
                " filename, media_type, size, sha256, created_by, created_at)"
                " VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?)",
                (
                    attachment_id,
                    conversation_id,
                    filename,
                    media_type,
                    staged.size,
                    staged.sha256,
                    actor,
                    ts(tx.now),
                ),
            )
            await record(
                tx,
                actor,
                "conversation",
                conversation_id,
                "conversation.updated",
                {
                    "attachment_id": attachment_id,
                    "filename": filename,
                    "size": staged.size,
                },
            )
            store.promote(staged, store_key(conversation_id, attachment_id))
            promoted = store.path(store_key(conversation_id, attachment_id))
    except BaseException:
        store.discard(staged)
        if promoted is not None:
            promoted.unlink(missing_ok=True)
        raise
    async with db.read() as conn:
        return await get_attachment(conn, conversation_id, attachment_id)


async def link(
    tx: Tx, conversation_id: str, message_id: str, attachment_ids: list[str]
) -> list[ChatAttachment]:
    if not attachment_ids:
        return []
    if len(attachment_ids) > MAX_PER_MESSAGE:
        raise ValidationFailedError(
            f"at most {MAX_PER_MESSAGE} attachments per message",
            code="attachment_limit",
        )
    if len(set(attachment_ids)) != len(attachment_ids):
        raise ValidationFailedError(
            "duplicate attachment id", code="duplicate_attachment"
        )
    q = ",".join("?" * len(attachment_ids))
    async with tx.conn.execute(
        f"SELECT id, conversation_id, message_id FROM message_attachments WHERE id IN ({q})",  # noqa: S608
        attachment_ids,
    ) as cur:
        rows = await cur.fetchall()
    found = {r["id"]: r for r in rows}
    for aid in attachment_ids:
        row = found.get(aid)
        if row is None:
            raise NotFoundError(f"attachment not found: {aid}")
        if row["conversation_id"] != conversation_id:
            raise ValidationFailedError(
                "attachment belongs to another conversation",
                code="wrong_conversation",
            )
        if row["message_id"] is not None:
            raise ConflictError(
                "attachment is already on a message", code="attachment_linked"
            )
    await tx.conn.execute(
        f"UPDATE message_attachments SET message_id = ? WHERE id IN ({q})",  # noqa: S608
        (message_id, *attachment_ids),
    )
    async with tx.conn.execute(
        "SELECT * FROM message_attachments WHERE message_id = ? ORDER BY created_at, id",
        (message_id,),
    ) as cur:
        return [_to_attachment(r) for r in await cur.fetchall()]


async def read_range(
    store: LocalArtifactStore,
    attachment: ChatAttachment,
    offset: int,
    max_bytes: int,
) -> ChatAttachmentText:
    path = store.path(store_key(attachment.conversation_id, attachment.id))
    if not is_text(attachment.media_type):
        async with await anyio.open_file(path, "rb") as f:
            await f.seek(offset)
            chunk = await f.read(max_bytes)
        nxt = offset + len(chunk)
        return ChatAttachmentText(
            attachment=attachment,
            offset=offset,
            length=len(chunk),
            next_offset=nxt if nxt < attachment.size else None,
            binary=True,
            text=None,
            content_base64=b64encode(chunk).decode("ascii") if chunk else "",
        )
    async with await anyio.open_file(path, "rb") as f:
        await f.seek(offset)
        chunk = await f.read(max_bytes)
    end = offset + len(chunk)
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    text = decoder.decode(chunk, final=end >= attachment.size)
    length = len(chunk) - len(decoder.getstate()[0])
    nxt = offset + length
    return ChatAttachmentText(
        attachment=attachment,
        offset=offset,
        length=length,
        next_offset=nxt if nxt < attachment.size else None,
        binary=False,
        text=text,
        content_base64=None,
    )
