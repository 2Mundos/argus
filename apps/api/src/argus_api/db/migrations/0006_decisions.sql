CREATE TABLE decisions (
    id            TEXT PRIMARY KEY,
    subject_type  TEXT NOT NULL,
    subject_id    TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('question', 'approval', 'review', 'policy')),
    question      TEXT NOT NULL,
    options       TEXT CHECK (options IS NULL OR json_valid(options)),
    audience      TEXT NOT NULL CHECK (audience IN ('human', 'orchestrator', 'any')),
    state         TEXT NOT NULL CHECK (state IN ('open', 'answered', 'cancelled')),
    answer_choice TEXT,
    answer_text   TEXT,
    requested_by  TEXT NOT NULL,
    decided_by    TEXT,
    created_at    TEXT NOT NULL,
    decided_at    TEXT,
    CHECK ((state = 'open') = (decided_at IS NULL))
) STRICT;

CREATE INDEX decisions_open ON decisions (created_at) WHERE state = 'open';
CREATE INDEX decisions_subject ON decisions (subject_type, subject_id, state);

-- Per-creator task rate (policy gate).
CREATE INDEX tasks_creator ON tasks (created_by, created_at);
