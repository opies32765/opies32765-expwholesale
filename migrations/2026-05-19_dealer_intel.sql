-- 2026-05-19 — Dealer DB Graph System: Dealer Intel layer
--
-- Per-vehicle keep/hold/price_drop/sell_now recommendation chip +
-- segment-level rollups (avg DOL, sold volume, market drift) used to
-- power the partner-portal Dealer Intel banner and per-row action chip.
--
-- Filled daily by dealer_intel.py, which runs AFTER encore_comps_pipeline
-- in the 23:00 EST cron (chained — comps data feeds intel).
--
-- Both tables are dealer-scoped via dealer_id. Currently only Encore
-- (dealer_id=8) is processed; widen by appending slug to
-- COMPS_ENABLED_SLUGS in partner_portal.py + updating the cron loop.
--
-- ─────────────────────────────────────────────────────────────────────
-- Per-VIN snapshot (one row per active inventory unit, latest only)
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dealer_intel_snapshot (
    dealer_inventory_id BIGINT      PRIMARY KEY REFERENCES dealer_inventory(id) ON DELETE CASCADE,
    dealer_id           INTEGER     NOT NULL REFERENCES dealers(id) ON DELETE CASCADE,
    vin                 TEXT        NOT NULL,
    snapshot_date       DATE        NOT NULL,

    -- Action recommendation
    chip                TEXT        NOT NULL,   -- 'keep' | 'hold' | 'price_drop' | 'sell_now'
    confidence          TEXT        NOT NULL,   -- 'high' | 'medium' | 'low'
    reasoning_text      TEXT        NOT NULL,   -- shown in hover/click tooltip

    -- Raw inputs that drove the rec (kept as JSONB so we can audit + show
    -- specific numbers in the tooltip without re-querying everything)
    inputs              JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Quick-access flat columns for sort/filter on portal queries
    days_on_lot         INTEGER,                -- NULL when first_seen_at unknown
    segment_avg_dol     NUMERIC(6,1),           -- segment-typical DOL we compared against
    segment_volume      INTEGER,                -- sold count in segment over window (drives confidence)
    asking_price        INTEGER,
    rbook_p50           NUMERIC,
    rbook_p75           NUMERIC,
    mmr_now             NUMERIC,
    mmr_trend_7d_pct    NUMERIC(6,2),
    mmr_trend_30d_pct   NUMERIC(6,2),

    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dealer_intel_snapshot_dealer
    ON dealer_intel_snapshot (dealer_id, chip);

CREATE INDEX IF NOT EXISTS idx_dealer_intel_snapshot_dealer_date
    ON dealer_intel_snapshot (dealer_id, snapshot_date DESC);

-- ─────────────────────────────────────────────────────────────────────
-- Segment rollups (per dealer × segment_key)
-- Segment granularity is intentionally coarse for low-volume dealers
-- (Encore moves ~1.6 units/day): make + year_band + mileage_band.
-- ─────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dealer_intel_segments (
    id              BIGSERIAL PRIMARY KEY,
    dealer_id       INTEGER     NOT NULL REFERENCES dealers(id) ON DELETE CASCADE,
    snapshot_date   DATE        NOT NULL,

    -- Segment dimensions
    segment_key     TEXT        NOT NULL,    -- 'mercedes-benz|2020-2023|0-40k'
    make            TEXT        NOT NULL,
    year_band       TEXT,                    -- 'pre-2015' | '2015-2019' | '2020-2023' | '2024+'
    mileage_band    TEXT,                    -- '0-40k' | '40k-80k' | '80k+' | 'unknown'

    -- Performance metrics over the analysis window
    window_days     INTEGER     NOT NULL,    -- e.g. 16
    sold_volume     INTEGER     NOT NULL DEFAULT 0,
    avg_dol_days    NUMERIC(6,1),
    median_dol_days NUMERIC(6,1),
    active_count    INTEGER     NOT NULL DEFAULT 0,
    aging_count     INTEGER     NOT NULL DEFAULT 0,  -- units past 1.2× segment avg DOL

    -- Market drift signals (NULL if not enough comp history yet)
    mmr_trend_7d_pct    NUMERIC(6,2),
    mmr_trend_30d_pct   NUMERIC(6,2),

    -- Overall verdict for the banner
    verdict         TEXT        NOT NULL,    -- 'strong' | 'normal' | 'slow' | 'avoid' | 'low_data'
    confidence      TEXT        NOT NULL,    -- 'high' | 'medium' | 'low'

    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (dealer_id, snapshot_date, segment_key)
);

CREATE INDEX IF NOT EXISTS idx_dealer_intel_segments_dealer_date
    ON dealer_intel_segments (dealer_id, snapshot_date DESC);
