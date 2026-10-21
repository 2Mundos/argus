-- Non-secret configuration of a `custom` provider account (docs/specs/custom-accounts.spec.md):
-- JSON {base_url, label, engine:{model, routing_options}, executor:{...}}. The API key
-- stays in the secret store; this column never holds it.
ALTER TABLE accounts ADD COLUMN config TEXT
    CHECK (config IS NULL OR json_valid(config));
