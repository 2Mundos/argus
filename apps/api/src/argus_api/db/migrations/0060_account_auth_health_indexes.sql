-- Bounded auth-health reads (account-usage.spec.md 3.5): last accepted / rejected.
CREATE INDEX account_usage_last_accepted ON account_usage (account_id, at)
  WHERE measurement = 'measured' AND output_tokens > 0;
CREATE INDEX events_account_auth_rejected ON events (subject_type, subject_id, kind, at)
  WHERE subject_type = 'account' AND kind = 'account.auth_rejected';
