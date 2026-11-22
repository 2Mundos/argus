-- The independent reviewer walks `execution.chains`; the separate `review.chains`
-- setting is gone, so its stored rows (global and project scope) are removed.
DELETE FROM settings WHERE key = 'review.chains';
