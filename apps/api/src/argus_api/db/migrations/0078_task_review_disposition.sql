-- How a task reached `succeeded` (task deliverable spec 3.10): accepted, accepted with
-- a recorded deviation, or not reviewed, with the acceptance items left unmet. JSON;
-- NULL for tasks that finished before it existed or have not succeeded.
ALTER TABLE tasks ADD COLUMN review_disposition TEXT
    CHECK (review_disposition IS NULL OR json_valid(review_disposition));
