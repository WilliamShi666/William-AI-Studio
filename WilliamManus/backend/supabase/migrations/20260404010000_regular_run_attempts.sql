-- Migration: add durable regular run attempts for Phase 2 supervisor execution

BEGIN;

ALTER TABLE public.agent_runs
    ADD COLUMN IF NOT EXISTS agent_run_id UUID;

UPDATE public.agent_runs
SET agent_run_id = gen_random_uuid()
WHERE agent_run_id IS NULL;

ALTER TABLE public.agent_runs
    ALTER COLUMN agent_run_id SET DEFAULT gen_random_uuid();

ALTER TABLE public.agent_runs
    ALTER COLUMN agent_run_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_index AS current_index
        JOIN pg_class AS table_class
            ON table_class.oid = current_index.indrelid
        JOIN pg_namespace AS table_namespace
            ON table_namespace.oid = table_class.relnamespace
        JOIN pg_attribute AS current_attribute
            ON current_attribute.attrelid = table_class.oid
           AND current_attribute.attnum = ANY(current_index.indkey)
        WHERE table_namespace.nspname = 'public'
          AND table_class.relname = 'agent_runs'
          AND current_index.indisunique
          AND current_index.indpred IS NULL
          AND current_index.indnkeyatts = 1
          AND current_attribute.attname = 'agent_run_id'
    ) THEN
        CREATE UNIQUE INDEX idx_agent_runs_agent_run_id
            ON public.agent_runs USING btree (agent_run_id);
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS public.regular_run_attempts (
    attempt_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_run_id UUID NOT NULL REFERENCES public.agent_runs(agent_run_id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    execution_epoch BIGINT NOT NULL,
    status TEXT NOT NULL,
    owner_token TEXT,
    supervisor_id TEXT,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT TIMEZONE('utc'::text, NOW()),
    claimed_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    terminal_status TEXT,
    terminal_reason TEXT,
    recovery_reason TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_regular_run_attempts_agent_run_id
    ON public.regular_run_attempts USING btree (agent_run_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_regular_run_attempts_agent_run_id_epoch
    ON public.regular_run_attempts USING btree (agent_run_id, execution_epoch);

CREATE INDEX IF NOT EXISTS idx_regular_run_attempts_status_queued_at
    ON public.regular_run_attempts USING btree (status, queued_at);

COMMIT;
