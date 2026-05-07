-- 2026-05-07 — LSL training data table
-- Joined view of LSL deals + inventory exposed for ML training pipelines.
-- Sourced from /opt/livesaleslog/crm.db (SQLite) by lsl_training_export.py,
-- refreshed daily via cron.
--
-- Coverage at creation: ~30K rows with 99% feature density on the key
-- market anchors (est_wholesale_price, market_asking_price, etc.).
--
-- Used by:
--   /opt/expwholesale/lsl_training_export.py   (writes here)
--   future ML scripts in /opt/expwholesale/ml/  (read-only)

CREATE TABLE IF NOT EXISTS lsl_training (
    -- Identity
    deal_id              INTEGER PRIMARY KEY,
    vin                  TEXT NOT NULL,
    inventory_id         INTEGER,
    stock_no             TEXT,

    -- Vehicle
    vehicle_info         TEXT,
    year                 INTEGER,
    make_name            TEXT,
    model_name           TEXT,
    series_name          TEXT,
    trim_name            TEXT,
    body_type            TEXT,
    exterior_color       TEXT,
    interior_color       TEXT,
    odometer             INTEGER,

    -- Market anchors (the gold)
    original_msrp        NUMERIC(10, 2),
    msrp                 NUMERIC(10, 2),
    est_wholesale_price  NUMERIC(10, 2),
    market_asking_price  NUMERIC(10, 2),
    asking_price         NUMERIC(10, 2),
    base_appraised_value NUMERIC(10, 2),
    mileage_adjustment_value NUMERIC(10, 2),

    -- Deal context
    sale_type            TEXT,
    vehicle_sale_type    TEXT,
    deal_type            TEXT,
    deal_status          TEXT,
    sales_person         TEXT,
    sales_manager        TEXT,

    -- Counterparties
    customer_name        TEXT,
    supplier_name        TEXT,
    source_name          TEXT,

    -- Timing
    sold_at              TIMESTAMPTZ,
    created_at_lsl       TIMESTAMPTZ,
    days_on_lot          INTEGER,
    days_since_purchase  INTEGER,

    -- Outcomes (training target + auxiliary)
    purchase_cost        NUMERIC(10, 2),  -- TARGET
    sale_price           NUMERIC(10, 2),
    front_value          NUMERIC(10, 2),
    deal_total_value     NUMERIC(10, 2),
    transport_fee        NUMERIC(10, 2),
    recon_cost           NUMERIC(10, 2),

    -- Derived ratios (precomputed for downstream ease)
    purchase_to_wholesale_ratio NUMERIC(8, 4),
    purchase_to_retail_ratio    NUMERIC(8, 4),
    sale_to_purchase_ratio      NUMERIC(8, 4),
    gross_dollars               NUMERIC(10, 2),

    -- Bookkeeping
    refreshed_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lsl_training_make
    ON lsl_training(make_name);
CREATE INDEX IF NOT EXISTS idx_lsl_training_make_year
    ON lsl_training(make_name, year);
CREATE INDEX IF NOT EXISTS idx_lsl_training_sold_at
    ON lsl_training(sold_at);
CREATE INDEX IF NOT EXISTS idx_lsl_training_vin
    ON lsl_training(vin);
