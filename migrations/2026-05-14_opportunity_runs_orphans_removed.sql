-- Adds orphans_removed counter to opportunity_runs.
-- Set when Phase 5b of the pipeline cleans up stale same-day rows
-- (rows whose VIN no longer qualifies AND status is still untouched).
-- Idempotent: safe to re-run.
ALTER TABLE opportunity_runs
  ADD COLUMN IF NOT EXISTS orphans_removed INTEGER DEFAULT 0;
