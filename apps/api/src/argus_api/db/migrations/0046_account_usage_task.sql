-- Yield per task (fleet health): usage rows are read by task id.
CREATE INDEX account_usage_task ON account_usage (task_id) WHERE task_id IS NOT NULL;
