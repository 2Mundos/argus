-- Project runtime images (docs/specs/project-runtime.spec.md): what each project's
-- tasks need in their agent image, the derived images built for it, and which image
-- each task attempt was given and actually ran in.

-- The declared requirement: component ids from the approved catalogue, never
-- Dockerfile text. One row per project; absent means "declares nothing".
CREATE TABLE project_runtimes (
    project    TEXT PRIMARY KEY REFERENCES projects (slug),
    spec       TEXT NOT NULL CHECK (json_valid(spec)),
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

-- Which daemon, platform, base image and uid a worker reported on its last prepare.
-- It scopes the claim gate to the host that would run the task.
CREATE TABLE runtime_hosts (
    agent_id   TEXT PRIMARY KEY REFERENCES agents (id) ON DELETE CASCADE,
    host_id    TEXT NOT NULL,
    run_as     TEXT NOT NULL,
    base_ref   TEXT NOT NULL,
    base_id    TEXT NOT NULL,
    platform   TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

-- One derived image per (project, content key, daemon, uid). The key is the hash of
-- base image id + rendered recipe + helper + platform, so a build is reused exactly
-- while those are equal. `ready` only after every probe passed (evidence).
CREATE TABLE runtime_builds (
    project       TEXT NOT NULL REFERENCES projects (slug),
    build_key     TEXT NOT NULL,
    host_id       TEXT NOT NULL,
    run_as        TEXT NOT NULL,
    recipe_digest TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (state IN ('preparing', 'building', 'verifying', 'ready', 'failed')),
    reason        TEXT,
    recipe        TEXT NOT NULL CHECK (json_valid(recipe)),
    base_ref      TEXT NOT NULL,
    base_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    image_ref     TEXT NOT NULL,
    image_id      TEXT,
    evidence      TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(evidence)),
    attempts      INTEGER NOT NULL DEFAULT 0,
    working_until TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    finished_at   TEXT,
    PRIMARY KEY (project, build_key, host_id, run_as),
    CHECK (state != 'ready' OR (image_id IS NOT NULL AND finished_at IS NOT NULL AND reason IS NULL))
) STRICT;

CREATE INDEX runtime_builds_open ON runtime_builds (state, created_at) WHERE state IN ('preparing', 'building', 'verifying');
CREATE INDEX runtime_builds_gate ON runtime_builds (project, host_id, run_as, recipe_digest, updated_at);

-- The image a task attempt was handed and the one its session container ran.
CREATE TABLE task_runtimes (
    task_id       TEXT NOT NULL REFERENCES tasks (id),
    attempt       INTEGER NOT NULL,
    project       TEXT NOT NULL,
    build_key     TEXT NOT NULL,
    image_ref     TEXT NOT NULL,
    image_id      TEXT NOT NULL,
    used_image_id TEXT,
    status        TEXT NOT NULL CHECK (status IN ('selected', 'verified', 'mismatch')),
    selected_at   TEXT NOT NULL,
    verified_at   TEXT,
    PRIMARY KEY (task_id, attempt)
) STRICT;
