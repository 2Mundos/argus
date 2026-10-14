-- Append-only audit log; every state transition writes one row in its transaction.
-- Timestamps are ISO-8601 UTC text with fixed width, so they sort and compare as text.
CREATE TABLE events (
    id           INTEGER PRIMARY KEY,
    at           TEXT NOT NULL,
    actor        TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    data         TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data))
) STRICT;

CREATE INDEX events_subject ON events (subject_type, subject_id, id);
