-- Chat file attachments (docs/specs/internal-orchestrator.spec.md 3.2).
-- Bytes live in the artifact store at conversations/{conversation_id}/{id}.
-- message_id is null until the file is sent with a human message.
CREATE TABLE message_attachments (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations (id),
    message_id TEXT REFERENCES messages (id),
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX message_attachments_conversation
    ON message_attachments (conversation_id, created_at, id);
CREATE INDEX message_attachments_message
    ON message_attachments (message_id, created_at, id)
    WHERE message_id IS NOT NULL;
