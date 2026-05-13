-- Schema for dealer opportunity engine (Phase A + Phase B/C columns wired up front).
-- Idempotent: safe to re-run.

BEGIN;

-- ── dealer_mmr ──────────────────────────────────────────────────────────────
-- Per-VIN MMR snapshot cache. Refreshed daily by the opportunity pipeline.
-- One row per VIN; UPSERT on each pull.
CREATE TABLE IF NOT EXISTS dealer_mmr (
  vin               TEXT PRIMARY KEY,
  dealer_id         INTEGER REFERENCES dealers(id) ON DELETE SET NULL,
  inventory_id      INTEGER,
  year              INTEGER,
  make              TEXT,
  model             TEXT,
  trim              TEXT,
  mileage           INTEGER,
  asking_price      INTEGER,

  -- Manheim wholesale (auction floor)
  wholesale_avg     INTEGER,
  wholesale_above   INTEGER,
  wholesale_below   INTEGER,
  -- Manheim retail-equivalent
  retail_avg        INTEGER,
  retail_above      INTEGER,
  retail_below      INTEGER,

  grade             TEXT,
  average_odometer  INTEGER,
  sample_size       INTEGER,
  no_mmr            BOOLEAN DEFAULT FALSE,  -- TRUE when vAuto returned no pricing
  fetch_error       TEXT,                    -- non-NULL when network/4xx/5xx
  raw               JSONB,

  fetched_at        TIMESTAMPTZ DEFAULT NOW(),
  updated_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_dealer_mmr_dealer  ON dealer_mmr(dealer_id);
CREATE INDEX IF NOT EXISTS idx_dealer_mmr_fetched ON dealer_mmr(fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_dealer_mmr_makemodel ON dealer_mmr(make, model);


-- ── dealer_opportunities ────────────────────────────────────────────────────
-- One row per (vin, snapshot_date). Pipeline writes daily; status workflow is
-- per-row (so today's opportunity for a VIN is tracked separately from
-- tomorrow's even if the VIN reappears).
CREATE TABLE IF NOT EXISTS dealer_opportunities (
  id                SERIAL PRIMARY KEY,
  snapshot_date     DATE NOT NULL DEFAULT CURRENT_DATE,

  vin               TEXT NOT NULL,
  dealer_id         INTEGER NOT NULL REFERENCES dealers(id) ON DELETE CASCADE,
  inventory_id      INTEGER,

  -- Vehicle identity snapshot (denormalized so the card renders even if
  -- dealer_inventory drops the row later)
  year              INTEGER,
  make              TEXT,
  model             TEXT,
  trim              TEXT,
  mileage           INTEGER,
  ext_color         TEXT,
  photo_url         TEXT,
  detail_url        TEXT,

  -- Pricing snapshot
  asking_price      INTEGER,
  mmr_wholesale_avg INTEGER,
  mmr_wholesale_above INTEGER,
  mmr_wholesale_below INTEGER,
  mmr_grade         TEXT,
  dollars_under_mmr INTEGER,        -- mmr_wholesale_avg - asking_price (positive = underpriced)
  pct_under_mmr     NUMERIC(5,2),   -- 100 * dollars_under_mmr / mmr_wholesale_avg

  -- rBook competitive set signals (Phase A.5)
  rbook_comp_count  INTEGER,
  rbook_p25         INTEGER,
  rbook_p50         INTEGER,
  rbook_p75         INTEGER,
  rbook_avg_dol     NUMERIC(5,1),
  retail_headroom   INTEGER,        -- rbook_p50 - asking_price (positive = retail margin)

  -- Dealer-side motivation signals
  dealer_dol        INTEGER,        -- days the car has been on dealer's site (verified or fallback)
  recent_price_drop_amount INTEGER,
  recent_price_drop_days_ago INTEGER,

  -- LSL pattern (Phase B)
  lsl_deal_count    INTEGER,
  lsl_avg_gross     INTEGER,

  -- Composite score 0-100
  score             INTEGER,
  score_breakdown   JSONB,           -- per-signal contributions for debug + UI tooltip

  -- Catch-all bag for everything else (Carfax flags, AccuTrade target_auction, etc.)
  signals           JSONB DEFAULT '{}'::jsonb,

  -- AI narrative (Phase B, lazy)
  gemini_pitch      TEXT,
  gemini_pitch_at   TIMESTAMPTZ,

  -- Workflow
  status            TEXT NOT NULL DEFAULT 'new'
                    CHECK (status IN ('new','called','pursuing','passed','acquired','snoozed')),
  assigned_to       TEXT,
  notes             TEXT,
  status_updated_at TIMESTAMPTZ,
  status_updated_by TEXT,

  created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One opportunity per VIN per snapshot day. Daily re-run upserts.
CREATE UNIQUE INDEX IF NOT EXISTS idx_opp_vin_day ON dealer_opportunities(vin, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_opp_score   ON dealer_opportunities(snapshot_date DESC, score DESC);
CREATE INDEX IF NOT EXISTS idx_opp_status  ON dealer_opportunities(status, snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_opp_dealer  ON dealer_opportunities(dealer_id, snapshot_date DESC);


-- ── opportunity_runs ────────────────────────────────────────────────────────
-- Audit + health view of pipeline executions.
CREATE TABLE IF NOT EXISTS opportunity_runs (
  id                SERIAL PRIMARY KEY,
  started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at       TIMESTAMPTZ,
  mmr_attempted     INTEGER,
  mmr_ok            INTEGER,
  mmr_no_data       INTEGER,
  mmr_errors        INTEGER,
  candidates_5pct   INTEGER,
  rbook_attempted   INTEGER,
  rbook_ok          INTEGER,
  rbook_errors      INTEGER,
  opportunities_written INTEGER,
  auth_failed       BOOLEAN DEFAULT FALSE,
  notes             TEXT
);

COMMIT;
