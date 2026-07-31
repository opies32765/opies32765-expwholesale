-- VAUTO_CLAIM_GATE_2026_07_31
-- A worker whose vAuto login is dead (per-profile Cox SSO cookie lost) still
-- reports chrome_alive=t and consecutive_failures=0, so nothing stops it
-- claiming bids. It then burns ~180s per claim before the watchdog releases,
-- and the bid re-queues to a healthy worker. Measured cost on 2026-07-30:
-- 24s when a bid hit a healthy worker first, 211-732s when it didn't.
--
-- These columns hold the OBSERVED gate state. The gate decision itself is
-- recomputed live from worker_jobs on every poll, so it self-clears the
-- moment the worker succeeds again -- these are for the /admin/workers
-- display and for one-shot email alerting, not for the decision.
ALTER TABLE workers
  ADD COLUMN IF NOT EXISTS vauto_gated_at         timestamptz,
  ADD COLUMN IF NOT EXISTS vauto_gate_reason      text,
  ADD COLUMN IF NOT EXISTS vauto_gate_notified_at timestamptz,
  ADD COLUMN IF NOT EXISTS vauto_gate_cleared_at  timestamptz,
  ADD COLUMN IF NOT EXISTS vauto_gate_blocks      integer NOT NULL DEFAULT 0;

-- The gate reads the last N completed vauto jobs per worker on every poll.
-- Without this the plan is a seq scan over worker_jobs on a hot path.
CREATE INDEX IF NOT EXISTS idx_worker_jobs_gate
    ON worker_jobs (worker_id, completed_at DESC)
 WHERE job_type = 'vauto' AND completed_at IS NOT NULL;
