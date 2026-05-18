-- 2026-05-18 — Daily comp-history series for partner-portal trend math
--
-- Per-day immutable history of MMR + rBook signals per (vin, snapshot_date),
-- plus the dealer's asking price observed that day. Trend math (7d/14d/30d/
-- 60d up vs down vs flat) is computed at portal render time by comparing
-- the newest row to the row closest to (today - N days).
--
-- Designed so we can answer the partner-portal question
--   "is THIS car drifting with the market or fighting it?"
-- by pairing the asking-price delta with the MMR/rBook delta over the
-- same window.
--
-- Filled by encore_comps_pipeline.py (10:30 ET cron). Day-1 backfill
-- pulls ~5 weeks of ManheimTransactions per VIN, so MMR-side trend
-- chips light up immediately; rBook trend builds forward.

CREATE TABLE IF NOT EXISTS dealer_inventory_comp_history (
    id                 BIGSERIAL PRIMARY KEY,
    dealer_inventory_id BIGINT      NOT NULL REFERENCES dealer_inventory(id) ON DELETE CASCADE,
    vin                TEXT         NOT NULL,
    snapshot_date      DATE         NOT NULL,
    asking_price       NUMERIC,
    mmr_wholesale_avg  NUMERIC,
    mmr_retail_avg     NUMERIC,
    mmr_sample_size    INTEGER,
    rbook_p25          NUMERIC,
    rbook_p50          NUMERIC,
    rbook_p75          NUMERIC,
    rbook_comp_count   INTEGER,
    rbook_avg_dol      NUMERIC,
    manheim_tx_count   INTEGER,
    manheim_tx_window_days INTEGER,
    source             TEXT,        -- 'manheim_tx_backfill' | 'daily_run' | 'inventory_history'
    raw                JSONB,
    computed_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (dealer_inventory_id, snapshot_date, source)
);

CREATE INDEX IF NOT EXISTS idx_dich_inv_date
    ON dealer_inventory_comp_history (dealer_inventory_id, snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_dich_vin_date
    ON dealer_inventory_comp_history (vin, snapshot_date DESC);
