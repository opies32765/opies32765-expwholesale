-- Worker claim queue + observability
-- Adds primary/standby dispatch with state-aware overflow:
--   * primary worker (Trainer / Beelink) claims any unclaimed work
--   * standby workers only claim when primary is busy or silent (>90s)
-- All queries use FOR UPDATE SKIP LOCKED for atomic, race-free dispatch.

BEGIN;

-- Bids: track which worker owns the in-flight vAuto lookup.
ALTER TABLE bids
  ADD COLUMN IF NOT EXISTS vauto_claimed_by  TEXT,
  ADD COLUMN IF NOT EXISTS vauto_claimed_at  TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_bids_vauto_claim
  ON bids (vauto_claimed_by, vauto_claimed_at)
  WHERE vauto_claimed_at IS NOT NULL;

-- Inventory: track which worker owns the in-flight rBook days verification.
ALTER TABLE dealer_inventory
  ADD COLUMN IF NOT EXISTS verify_claimed_by  TEXT,
  ADD COLUMN IF NOT EXISTS verify_claimed_at  TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_inventory_verify_claim
  ON dealer_inventory (verify_claimed_by, verify_claimed_at)
  WHERE verify_claimed_at IS NOT NULL;

-- Workers: heartbeat + role/priority registry.
CREATE TABLE IF NOT EXISTS workers (
    worker_id        TEXT PRIMARY KEY,
    role             TEXT,                    -- 'ew_worker' | 'verifier' | 'carhub'
    priority         TEXT DEFAULT 'standby',  -- 'primary' | 'standby'
    last_heartbeat   TIMESTAMPTZ DEFAULT NOW(),
    chrome_alive     BOOLEAN DEFAULT TRUE,
    lookups_done     INT DEFAULT 0,
    last_lookup_at   TIMESTAMPTZ,
    last_seen_ip     INET,
    notes            TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    updated_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_workers_priority_heartbeat
  ON workers (priority, last_heartbeat DESC);

-- Job history (look-back queue for the dashboard).
CREATE TABLE IF NOT EXISTS worker_jobs (
    id            BIGSERIAL PRIMARY KEY,
    bid_id        INT,                   -- vAuto/AccuTrade/iPacket worker jobs
    inventory_id  INT,                   -- verifier jobs
    worker_id     TEXT NOT NULL,
    job_type      TEXT NOT NULL,         -- 'vauto' | 'accutrade' | 'ipacket' | 'verify'
    claimed_at    TIMESTAMPTZ DEFAULT NOW(),
    completed_at  TIMESTAMPTZ,
    status        TEXT,                  -- 'ok' | 'failed' | 'released_stale' | 'in_progress'
    duration_ms   INT,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_worker_jobs_worker
  ON worker_jobs (worker_id, claimed_at DESC);
CREATE INDEX IF NOT EXISTS idx_worker_jobs_bid
  ON worker_jobs (bid_id) WHERE bid_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_worker_jobs_inv
  ON worker_jobs (inventory_id) WHERE inventory_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_worker_jobs_active
  ON worker_jobs (worker_id, completed_at) WHERE completed_at IS NULL;

-- Seed primary workers so standby logic finds them. Heartbeat will be
-- updated by their first /heartbeat POST; setting NOW() here lets the
-- standby liveness check pass even before the first heartbeat lands.
INSERT INTO workers (worker_id, role, priority, last_heartbeat)
VALUES
    ('trainer',  'ew_worker', 'primary', NOW()),
    ('beelink',  'verifier',  'primary', NOW())
ON CONFLICT (worker_id) DO UPDATE SET
    priority = EXCLUDED.priority,
    role     = COALESCE(workers.role, EXCLUDED.role);

COMMIT;
