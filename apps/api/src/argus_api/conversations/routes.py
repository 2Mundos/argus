"""REST routes for conversations and the agent chat channel
(docs/specs/internal-orchestrator.spec.md 3.2-3.3)."""

from __future__ import annotations

import asyncio
from typing import Annotated

from argus_core.auth import Scope
from argus_core.conversations import (
    AgentChatClaim,
    AgentChatReply,
    ChatContext,
    ChatOriginTask,
    ChatSessionPut,
    Conversation,
    ConversationCreate,
    ConversationList,
    ConversationPatch,
    Message,
    MessageCreate,
    MessageList,
)
from fastapi import APIRouter, Query, Request

from argus_api.conversations import service
from argus_api.deps import (
    AdminDep,
    AgentDep,
    CallerDep,
    DbDep,
    SettingsDep,
    TokenDep,
)
from argus_api.errors import ForbiddenError
from argus_api.orchestrator import agent_system_pool

router = APIRouter(tags=["conversations"])


def _require_admin(caller) -> None:
    if caller.scope is not Scope.ADMIN:
        raise ForbiddenError("conversations are admin-only")


@router.get("/conversations")
async def list_conversations(
    db: DbDep, caller: CallerDep, archived: bool = False
) -> ConversationList:
    _require_admin(caller)
    async with db.read() as conn:
        items = await service.list_conversations(conn, include_archived=archived)
    return ConversationList(items=items)


@router.post("/conversations")
async def create_conversation(
    body: ConversationCreate, db: DbDep, caller: AdminDep
) -> Conversation:
    async with db.write() as tx:
        return await service.create_conversation(tx, body, caller.actor)


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str, db: DbDep, caller: CallerDep
) -> Conversation:
    _require_admin(caller)
    async with db.read() as conn:
        return await service.get_conversation(conn, conversation_id)


@router.patch("/conversations/{conversation_id}")
async def patch_conversation(
    conversation_id: str, body: ConversationPatch, db: DbDep, caller: AdminDep
) -> Conversation:
    async with db.write() as tx:
        return await service.patch_conversation(tx, conversation_id, body, caller.actor)


@router.post("/conversations/{conversation_id}/archive")
async def archive_conversation(
    conversation_id: str, db: DbDep, caller: AdminDep
) -> Conversation:
    async with db.write() as tx:
        return await service.archive_conversation(
            tx, conversation_id, caller.actor, archive=True
        )


@router.post("/conversations/{conversation_id}/unarchive")
async def unarchive_conversation(
    conversation_id: str, db: DbDep, caller: AdminDep
) -> Conversation:
    async with db.write() as tx:
        return await service.archive_conversation(
            tx, conversation_id, caller.actor, archive=False
        )


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    db: DbDep,
    caller: CallerDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> MessageList:
    _require_admin(caller)
    async with db.read() as conn:
        items = await service.list_messages(conn, conversation_id, limit=limit)
    return MessageList(items=items)


@router.post("/conversations/{conversation_id}/messages")
async def post_message(
    conversation_id: str,
    body: MessageCreate,
    db: DbDep,
    caller: AdminDep,
    token: TokenDep,
) -> Message:
    async with db.write() as tx:
        return await service.post_human_message(
            tx, conversation_id, body, caller.actor, secrets=(token,) if token else ()
        )


# -- agent chat channel (orchestrator tokens of the system pool) ---------------------


async def _require_internal_orchestrator(db, agent_id: str) -> None:
    async with db.read() as conn:
        if await agent_system_pool(conn, agent_id) != "orchestrator":
            raise ForbiddenError(
                "chat channel is only for the internal orchestrator",
                code="not_internal_orchestrator",
            )


@router.get("/agents/me/chat")
async def claim_chat(
    request: Request,
    db: DbDep,
    settings: SettingsDep,
    agent_id: AgentDep,
    wait_s: Annotated[float, Query(ge=0, le=30)] = 0,
) -> AgentChatClaim | None:
    """Long-poll for the next queued human message (claim_task pattern)."""
    await _require_internal_orchestrator(db, agent_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_s
    while True:
        # Idle polls only read; the writer is taken when a message can be delivered.
        async with db.read() as conn:
            ready = await service.has_claimable(
                conn, db.now(), settings.model_policy_path
            )
        if ready:
            async with db.write() as tx:
                claimed = await service.claim_next(
                    tx, agent_id=agent_id, path=settings.model_policy_path
                )
            if claimed is not None:
                conv, msg = claimed
                return AgentChatClaim(message=msg, conversation=conv)
        if wait_s <= 0 or loop.time() >= deadline:
            return None
        await asyncio.sleep(0.5)


@router.post("/agents/me/chat/{conversation_id}")
async def post_chat_reply(
    conversation_id: str,
    body: AgentChatReply,
    db: DbDep,
    settings: SettingsDep,
    agent_id: AgentDep,
    token: TokenDep,
) -> Message:
    await _require_internal_orchestrator(db, agent_id)
    async with db.write() as tx:
        return await service.post_reply(
            tx,
            conversation_id,
            body,
            agent_id,
            secrets=(token,) if token else (),
            pricing_file=settings.pricing_file,
        )


@router.get("/agents/me/chat/{conversation_id}/context")
async def chat_context(
    conversation_id: str, db: DbDep, agent_id: AgentDep
) -> ChatContext:
    """Bounded history for seeding a new session after restart (3.4)."""
    await _require_internal_orchestrator(db, agent_id)
    async with db.read() as conn:
        messages, tasks = await service.chat_context(conn, conversation_id)
    return ChatContext(
        messages=messages,
        tasks=[ChatOriginTask(id=i, title=t, state=s) for i, t, s in tasks],
    )


@router.put("/agents/me/chat/{conversation_id}/session")
async def put_chat_session(
    conversation_id: str, body: ChatSessionPut, db: DbDep, agent_id: AgentDep
) -> Conversation:
    """Remember the NanoClaw session id for resume (3.4)."""
    await _require_internal_orchestrator(db, agent_id)
    async with db.write() as tx:
        await service.set_session_ref(tx, conversation_id, body.session_ref)
        if body.restored:
            await service.note_session_restored(tx, conversation_id)
        return await service.get_conversation(tx.conn, conversation_id)
