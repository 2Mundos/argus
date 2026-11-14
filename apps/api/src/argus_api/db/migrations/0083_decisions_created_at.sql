-- Fleet health reads decisions opened in a window by created_at (fleet-health spec).
CREATE INDEX decisions_created ON decisions (created_at);
