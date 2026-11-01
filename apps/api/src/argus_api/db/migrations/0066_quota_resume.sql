-- Quota resume (docs/specs/quota-resume.spec.md): the native session a quota pause
-- left behind, as the paused attempt's row. `native_session` is JSON
-- {session_id, engine, node}, written only by a quota pause. `native_session_used_by`
-- is the attempt that took it (resumed or fell back), so it is offered once.
ALTER TABLE task_attempts ADD COLUMN native_session TEXT;
ALTER TABLE task_attempts ADD COLUMN native_session_used_by INTEGER;
