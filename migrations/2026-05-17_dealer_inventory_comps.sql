-- 2026-05-17 — Per-unit market comps for Partner Portal
-- Scoped initially to Encore Motorcars of Sarasota (dealer_id=8). The
-- script is per-dealer; only the Encore cron is scheduled.
--
-- Populated nightly by scripts/comps_daily_encore.py at 02:30 ET.
-- Read by partner_portal.dashboard() and rendered on partner_dashboard.html
-- under each inventory row.

CREATE TABLE IF NOT EXISTS dealer_inventory_comps (
    dealer_inventory_id      BIGINT       NOT NULL REFERENCES dealer_inventory(id) ON DELETE CASCADE,
    snapshot_date            DATE         NOT NULL DEFAULT CURRENT_DATE,
    mmr_comp_value           NUMERIC,
    mmr_comp_count           INTEGER,
    rbook_p25                NUMERIC,
    rbook_p50                NUMERIC,
    rbook_comp_count         INTEGER,
    market_median_days_on_lot INTEGER,
    market_median_source     TEXT,
    price_trend_7d           JSONB,
    price_trend_14d          JSONB,
    price_trend_30d          JSONB,
    price_trend_60d          JSONB,
    comps_raw                JSONB,
    computed_at              TIMESTAMPTZ  DEFAULT NOW(),
    PRIMARY KEY (dealer_inventory_id, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_dic_snapshot
    ON dealer_inventory_comps (snapshot_date DESC);
CREATE INDEX IF NOT EXISTS idx_dic_inv_latest
    ON dealer_inventory_comps (dealer_inventory_id, snapshot_date DESC);
