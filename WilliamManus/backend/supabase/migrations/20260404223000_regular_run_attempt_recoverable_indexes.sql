BEGIN;

CREATE INDEX IF NOT EXISTS idx_regular_run_attempts_recoverable_lease
    ON public.regular_run_attempts USING btree (status, lease_expires_at)
    WHERE status IN ('claimed', 'running');

CREATE INDEX IF NOT EXISTS idx_regular_run_attempts_run_recoverable_epoch
    ON public.regular_run_attempts USING btree (
        agent_run_id,
        execution_epoch DESC,
        attempt_number DESC
    )
    WHERE status IN ('claimed', 'running');

COMMIT;
