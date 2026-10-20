-- What actually runs, as opposed to what a pool asks for (MCP operations 3.3).
-- The image an instance was started with, and its digest as the target reports
-- it once the container runs (never from the worker itself).
ALTER TABLE instances ADD COLUMN image TEXT;
ALTER TABLE instances ADD COLUMN image_digest TEXT;
-- What an agent reports about itself on heartbeat (version, engine, executor).
ALTER TABLE agents ADD COLUMN runtime TEXT CHECK (runtime IS NULL OR json_valid(runtime));
