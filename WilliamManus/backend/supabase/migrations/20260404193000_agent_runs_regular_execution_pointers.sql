-- Migration: add parent-run execution pointers for phase3 regular supervisor serving paths

BEGIN;

ALTER TABLE public.agent_runs
    ADD COLUMN IF NOT EXISTS active_attempt_id UUID,
    ADD COLUMN IF NOT EXISTS current_execution_epoch BIGINT,
    ADD COLUMN IF NOT EXISTS stream_source_epoch BIGINT,
    ADD COLUMN IF NOT EXISTS active_supervisor_id TEXT,
    ADD COLUMN IF NOT EXISTS regular_execution_backend TEXT;

CREATE INDEX IF NOT EXISTS idx_agent_runs_regular_execution_backend
    ON public.agent_runs USING btree (regular_execution_backend);

COMMIT;
