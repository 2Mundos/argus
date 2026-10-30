"""Conversation and chat message models (internal-orchestrator.spec.md 3.2)."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ConversationKind(StrEnum):
    CHAT = "chat"
    UPDATES = "updates"


class TitleSource(StrEnum):
    ORCHESTRATOR = "orchestrator"
    ADMIN = "admin"


class MessageState(StrEnum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    ANSWERED = "answered"
    POSTED = "posted"


class Conversation(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: str
    kind: ConversationKind
    title: str
    title_source: TitleSource
    created_by: str
    archived_at: datetime | None
    session_ref: str | None
    created_at: datetime
    updated_at: datetime
    cost_usd: float | None = None  # estimated spend attributed to this thread


class Message(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    id: str
    conversation_id: str
    author: str
    body: str
    state: MessageState
    truncated: bool = False
    turn_id: str | None = None
    created_at: datetime
    delivered_at: datetime | None = None


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)


class ConversationPatch(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class MessageCreate(BaseModel):
    body: str = Field(min_length=1, max_length=8000)


class AgentChatClaim(BaseModel):
    """One inbound human message for the orchestrator's channel."""

    message: Message
    conversation: Conversation


class AgentChatReply(BaseModel):
    body: str = Field(min_length=1, max_length=65536)
    turn_id: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    usage: "TurnUsage | None" = None


class TurnUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    model: str | None = None


class ConversationList(BaseModel):
    items: list[Conversation]


class MessageList(BaseModel):
    items: list[Message]


class ChatSessionPut(BaseModel):
    """NanoClaw session id for resume after a container recreate (3.4)."""

    session_ref: str = Field(min_length=1, max_length=200)
    # The session was rebuilt from a history excerpt: the thread shows a system note.
    restored: bool = False


class ChatOriginTask(BaseModel):
    id: str
    title: str
    state: str


class ChatContext(BaseModel):
    """Bounded seed for a new session when session_ref cannot be resumed."""

    messages: list[Message]
    tasks: list[ChatOriginTask]


AgentChatReply.model_rebuild()
