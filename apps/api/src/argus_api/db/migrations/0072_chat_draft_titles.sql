-- Chats still titled with the old placeholder take the first line of their first admin
-- message, as new chats now do (docs/specs/internal-orchestrator.spec.md 3.2). Chats
-- an admin renamed, the Updates thread and chats with no message are left alone.
UPDATE conversations
SET title = (
    SELECT CASE WHEN length(line) > 60 THEN rtrim(substr(line, 1, 59)) || '…' ELSE line END
    FROM (
        SELECT trim(
                   CASE WHEN instr(body, char(10)) > 0
                        THEN substr(body, 1, instr(body, char(10)) - 1)
                        ELSE body END,
                   ' ' || char(9) || char(13)
               ) AS line
        FROM messages
        WHERE conversation_id = conversations.id AND author LIKE 'admin:%'
        ORDER BY created_at, id
        LIMIT 1
    )
)
WHERE kind = 'chat' AND title = 'New chat' AND title_source = 'orchestrator'
  AND EXISTS (SELECT 1 FROM messages
              WHERE conversation_id = conversations.id AND author LIKE 'admin:%');
