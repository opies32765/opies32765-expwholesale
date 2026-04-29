-- Self-healing dispatch: auto-demote workers that fail repeatedly,
-- auto-promote them once synthetic checks confirm recovery, and let
-- workers self-report Cox-session loss so dispatch routes around them.
--
-- Sits on top of migrations/add_worker_claim_queue.sql.

BEGIN;

ALTER TABLE workers
  ADD COLUMN IF NOT EXISTS consecutive_failures INT         DEFAULT 0,
  ADD COLUMN IF NOT EXISTS paused                BOOLEAN    DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS pause_reason          TEXT,
  ADD COLUMN IF NOT EXISTS last_claim_status     TEXT,
  ADD COLUMN IF NOT EXISTS last_claim_duration_ms INT,
  ADD COLUMN IF NOT EXISTS auto_demoted_at       TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS synthetic_ok_count    INT         DEFAULT 0,
  ADD COLUMN IF NOT EXISTS last_synthetic_at     TIMESTAMPTZ;

-- Track a worker's "effective" priority. We keep the original `priority`
-- column as the operator's intent, and `effective_priority` is what
-- dispatch actually uses. Auto-demote sets effective='degraded' without
-- mutating the operator's choice; auto-promote restores it.
ALTER TABLE workers
  ADD COLUMN IF NOT EXISTS effective_priority TEXT;

UPDATE workers SET effective_priority = priority WHERE effective_priority IS NULL;

CREATE INDEX IF NOT EXISTS idx_workers_effective_priority
  ON workers (effective_priority, last_heartbeat DESC);

COMMIT;
