-- The account providers each agent's runtime can apply, as it last declared them on
-- a claim (account selection 3.6). Capacity counts only accounts of a provider some
-- live agent can run: a `custom` account no worker runs is not a free slot.
ALTER TABLE agents ADD COLUMN runtime_providers TEXT
    CHECK (runtime_providers IS NULL OR json_valid(runtime_providers));
