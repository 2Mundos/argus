-- Integration config edits (docs/specs/integrations.spec.md 3.4). Raised by every
-- edit of the non-secret config or access, as rotation raises secret_version: a
-- worker that installed an earlier config re-fetches it, its route grants fail.
ALTER TABLE integrations ADD COLUMN config_version INTEGER NOT NULL DEFAULT 1;
