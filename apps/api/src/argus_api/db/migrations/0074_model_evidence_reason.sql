-- Why a run reports no model (model-observability spec 3.1): set only with evidence
-- 'not_reported'; NULL while the run may still report one, and for rows that predate it.
ALTER TABLE model_executions ADD COLUMN evidence_reason TEXT CHECK (evidence_reason IS NULL OR
    (evidence = 'not_reported' AND evidence_reason IN ('engine_output_has_no_model',
        'executor_not_verified', 'engine_not_verified', 'run_not_started')));
