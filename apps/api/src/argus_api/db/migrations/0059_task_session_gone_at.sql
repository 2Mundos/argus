-- The automatic resume rule (docs/specs/failure-classes.spec.md 3.2) confirms an
-- `agent_ended` exit only when the host saw the session gone without a break for
-- the channel's 90 s grace: when the current run of `gone` observations began.
ALTER TABLE tasks ADD COLUMN session_gone_at TEXT;
