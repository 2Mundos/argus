-- Projects: the scope knowledge (and, optionally, tasks) belong to.
CREATE TABLE projects (
    slug        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
) STRICT;

INSERT INTO projects (slug, name, description, created_at, updated_at) VALUES (
    'global', 'Global', 'Fleet-wide knowledge: conventions, tooling, how Argus works.',
    '2026-10-14T00:00:00.000000Z', '2026-10-14T00:00:00.000000Z');

ALTER TABLE tasks ADD COLUMN project TEXT REFERENCES projects (slug);
CREATE INDEX tasks_project ON tasks (project, state);

-- Pages pinned to a task, delivered with the claim.
CREATE TABLE task_knowledge (
    task_id TEXT NOT NULL REFERENCES tasks (id),
    page_id TEXT NOT NULL,
    PRIMARY KEY (task_id, page_id)
) STRICT, WITHOUT ROWID;

-- A page's current title/tags/summary mirror its current revision, for listing.
CREATE TABLE knowledge_pages (
    id                  TEXT PRIMARY KEY,
    project             TEXT NOT NULL REFERENCES projects (slug),
    slug                TEXT NOT NULL,
    title               TEXT NOT NULL,
    summary             TEXT NOT NULL,
    tags                TEXT NOT NULL CHECK (json_valid(tags)),
    current_revision_id TEXT NOT NULL,
    current_number      INTEGER NOT NULL,
    state               TEXT NOT NULL CHECK (state IN ('active', 'archived')),
    reviewed_at         TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (project, slug)
) STRICT;

-- Immutable. Numbers are server-assigned and only ever grow per page.
CREATE TABLE knowledge_revisions (
    id         TEXT PRIMARY KEY,
    page_id    TEXT NOT NULL REFERENCES knowledge_pages (id),
    number     INTEGER NOT NULL,
    title      TEXT NOT NULL,
    summary    TEXT NOT NULL,
    body       TEXT NOT NULL,
    tags       TEXT NOT NULL CHECK (json_valid(tags)),
    author     TEXT NOT NULL,
    reason     TEXT NOT NULL,
    sources    TEXT NOT NULL CHECK (json_valid(sources)),
    created_at TEXT NOT NULL,
    UNIQUE (page_id, number)
) STRICT;

-- Full-text index over active pages' current revisions; maintained in the same
-- transaction as every publish/archive, so search never lags a committed page.
CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    page_id UNINDEXED,
    title,
    summary,
    body,
    tags,
    tokenize = 'porter unicode61'
);

CREATE TABLE knowledge_proposals (
    id               TEXT PRIMARY KEY,
    page_id          TEXT REFERENCES knowledge_pages (id),  -- NULL: a new page
    project          TEXT NOT NULL REFERENCES projects (slug),
    slug             TEXT NOT NULL,
    title            TEXT NOT NULL,
    summary          TEXT NOT NULL,
    body             TEXT NOT NULL,
    tags             TEXT NOT NULL CHECK (json_valid(tags)),
    base_revision_id TEXT,
    author           TEXT NOT NULL,
    reason           TEXT NOT NULL,
    sources          TEXT NOT NULL CHECK (json_valid(sources)),
    state            TEXT NOT NULL CHECK (state IN ('open', 'accepted', 'rejected', 'withdrawn')),
    created_at       TEXT NOT NULL
) STRICT;

-- Decisions gain the 'knowledge' kind. SQLite cannot alter a CHECK: rebuild.
CREATE TABLE decisions_new (
    id            TEXT PRIMARY KEY,
    subject_type  TEXT NOT NULL,
    subject_id    TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('question', 'approval', 'review', 'policy', 'knowledge')),
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
INSERT INTO decisions_new SELECT * FROM decisions;
DROP TABLE decisions;
ALTER TABLE decisions_new RENAME TO decisions;
CREATE INDEX decisions_open ON decisions (created_at) WHERE state = 'open';
CREATE INDEX decisions_subject ON decisions (subject_type, subject_id, state);
