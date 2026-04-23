-- =====================================================================
-- Dealer DB — Phase 1 schema (additive, safe to run on existing EW DB)
-- Run as postgres superuser: expuser lacks ALTER/CREATE privileges.
-- All CREATEs are IF NOT EXISTS; re-running is a no-op.
-- =====================================================================

-- ── dealers ──────────────────────────────────────────────────────────
-- Registry of scanned dealers. One row per dealer we've attached to EW.
CREATE TABLE IF NOT EXISTS dealers (
    id              SERIAL PRIMARY KEY,
    name            TEXT NOT NULL,
    url             TEXT NOT NULL UNIQUE,
    platform        TEXT,                    -- dealer.com, dealerinspire, autorevo, shopify, custom, etc.
    scrape_method   TEXT,                    -- jsonld, sitemap, platform_api, custom
    dia_dealer_id   INTEGER,                 -- optional link to DIA dealer_intelligence.dealers row
    city            TEXT,
    state           TEXT,
    phone           TEXT,
    notes           TEXT,
    active          BOOLEAN DEFAULT TRUE,
    preferred_tier  TEXT,                     -- pin a scrape tier per-dealer (e.g. 'direct_proxy' for hosts with IP-reputation issues). NULL = auto by platform.
    last_scan_at    TIMESTAMPTZ,
    last_scan_status TEXT,                   -- ok | blocked | error:<msg>
    last_scan_id    BIGINT,                  -- points at dealer_scans row
    retail_profile  JSONB,                   -- derived: avg price, make mix, turnover (Phase 2)
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dealers_active ON dealers(active);
CREATE INDEX IF NOT EXISTS idx_dealers_state  ON dealers(state);


-- ── dealer_inventory ─────────────────────────────────────────────────
-- Current live snapshot per dealer. We NEVER delete rows — only flip status.
-- Dedup is (dealer_id, VIN-or-URL) to handle dealers that hide VIN publicly.
CREATE TABLE IF NOT EXISTS dealer_inventory (
    id              SERIAL PRIMARY KEY,
    dealer_id       INTEGER NOT NULL REFERENCES dealers(id) ON DELETE CASCADE,
    vin             TEXT DEFAULT '',
    stock_number    TEXT,
    year            INTEGER,
    make            TEXT,
    model           TEXT,
    trim            TEXT,
    body_style      TEXT,
    ext_color       TEXT,
    int_color       TEXT,
    mileage         INTEGER,
    price           INTEGER,
    msrp            INTEGER,
    url             TEXT,
    photo_url       TEXT,                    -- primary / cover photo
    photos          JSONB,                   -- all photos
    -- Tracking fields
    first_seen_at   TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at    TIMESTAMPTZ DEFAULT NOW(),
    missing_scans   INTEGER DEFAULT 0,       -- incremented when VIN absent from a scan
    -- Price change
    last_price      INTEGER,                 -- previous scan's price, for diff rendering
    last_price_change_at TIMESTAMPTZ,
    price_drop_amount INTEGER,               -- populated when latest observed price < prior
    price_drop_at   TIMESTAMPTZ,
    -- Status lifecycle
    status          TEXT DEFAULT 'active',   -- active | missing | sold
    sold_at         TIMESTAMPTZ,
    sold_confidence NUMERIC(3,2),            -- 0.00–1.00 combined signal score
    sold_signals    JSONB,                   -- array of {type, detail, detected_at}
    -- Raw scrape payload (for reprocessing without re-scraping)
    raw             JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- VIN-or-URL dedup (matches DIA smart-merge pattern)
CREATE UNIQUE INDEX IF NOT EXISTS idx_dealer_inventory_dedup
    ON dealer_inventory(dealer_id, COALESCE(NULLIF(vin, ''), url));

-- VIN sanity guard: real VINs always contain at least one letter
-- (manufacturer + model-year codes are alphabetic). Without this, inline-JSON
-- numerics (Unix timestamps, photo IDs, listing-IDs) get captured by the
-- universal HTML extractor's `[A-HJ-NPR-Z0-9]{17}` regex and create phantom
-- duplicate inventory rows. TXT Charlie scan 33 (2026-04-23) inserted 60 such
-- rows before this guard was added — see git history for the cleanup.
DO $$ BEGIN
    BEGIN
        ALTER TABLE dealer_inventory
            ADD CONSTRAINT vin_has_letter_when_present
            CHECK (vin = '' OR vin IS NULL OR vin ~ '[A-Za-z]');
    EXCEPTION WHEN duplicate_object THEN
        -- already added — fine, this script is rerunnable
        NULL;
    END;
END $$;

CREATE INDEX IF NOT EXISTS idx_dealer_inventory_dealer_status
    ON dealer_inventory(dealer_id, status);
CREATE INDEX IF NOT EXISTS idx_dealer_inventory_dealer_make_model
    ON dealer_inventory(dealer_id, make, model);
CREATE INDEX IF NOT EXISTS idx_dealer_inventory_vin
    ON dealer_inventory(vin) WHERE vin <> '';
CREATE INDEX IF NOT EXISTS idx_dealer_inventory_first_seen
    ON dealer_inventory(first_seen_at);


-- ── dealer_inventory_history ─────────────────────────────────────────
-- Append-only observation log. One row per (vehicle, scan). Fuels graphs +
-- price trend analysis + matchback training data. Never updated, never deleted.
CREATE TABLE IF NOT EXISTS dealer_inventory_history (
    id              BIGSERIAL PRIMARY KEY,
    dealer_id       INTEGER NOT NULL REFERENCES dealers(id) ON DELETE CASCADE,
    inventory_id    INTEGER REFERENCES dealer_inventory(id) ON DELETE CASCADE,
    vin             TEXT,
    url             TEXT,
    price           INTEGER,
    mileage         INTEGER,
    observed_at     TIMESTAMPTZ DEFAULT NOW(),
    scan_id         BIGINT                   -- groups observations from a single scan run
);

CREATE INDEX IF NOT EXISTS idx_dih_dealer_vin ON dealer_inventory_history(dealer_id, vin);
CREATE INDEX IF NOT EXISTS idx_dih_scan ON dealer_inventory_history(scan_id);


-- ── dealer_scans ─────────────────────────────────────────────────────
-- One row per scan run. Status + counters for audit + stats.
CREATE TABLE IF NOT EXISTS dealer_scans (
    id              BIGSERIAL PRIMARY KEY,
    dealer_id       INTEGER NOT NULL REFERENCES dealers(id) ON DELETE CASCADE,
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    finished_at     TIMESTAMPTZ,
    status          TEXT,                    -- running | ok | blocked | error
    vehicles_found  INTEGER DEFAULT 0,
    new_count       INTEGER DEFAULT 0,
    sold_count      INTEGER DEFAULT 0,
    missing_count   INTEGER DEFAULT 0,
    price_drop_count INTEGER DEFAULT 0,
    platform_detected TEXT,
    error_message   TEXT,
    duration_ms     INTEGER
);

CREATE INDEX IF NOT EXISTS idx_dealer_scans_dealer_started
    ON dealer_scans(dealer_id, started_at DESC);


-- ── dealer_sold_signals ──────────────────────────────────────────────
-- Append-only sold-detection evidence. Multiple signals per VIN allowed.
-- The combined score on dealer_inventory.sold_confidence summarises these.
CREATE TABLE IF NOT EXISTS dealer_sold_signals (
    id              BIGSERIAL PRIMARY KEY,
    dealer_id       INTEGER NOT NULL REFERENCES dealers(id) ON DELETE CASCADE,
    inventory_id    INTEGER REFERENCES dealer_inventory(id) ON DELETE CASCADE,
    vin             TEXT,
    signal_type     TEXT NOT NULL,
    -- Signal types:
    --   missing_from_scan      VIN absent from current scan (weight 0.4 per consecutive)
    --   url_404                URL returns 404/410
    --   url_redirect_sold      URL redirects to a page containing sold-ish path
    --   text_sold              Page body regex match: \bsold\b
    --   text_pending           Page body match: \b(sale )?pending\b / under contract
    --   sitemap_missing        VIN dropped from /sitemap.xml
    --   ocr_photo_sold         OCR of photo text shows SOLD/PENDING (Phase 2)
    signal_detail   TEXT,
    confidence      NUMERIC(3,2) DEFAULT 0.50,
    detected_at     TIMESTAMPTZ DEFAULT NOW(),
    scan_id         BIGINT
);

CREATE INDEX IF NOT EXISTS idx_dss_inventory ON dealer_sold_signals(inventory_id);
CREATE INDEX IF NOT EXISTS idx_dss_dealer_vin ON dealer_sold_signals(dealer_id, vin);


-- ── Convenience view: dealer stats for list page ─────────────────────
-- Age buckets prefer dealer-declared source_added_at (JSON-LD datePosted /
-- photo-filename timestamp) over our scanner-observed first_seen_at.
-- COALESCE — when source_added_at is NULL we fall back to first_seen_at.
CREATE OR REPLACE VIEW dealer_stats AS
SELECT
    d.id                                         AS dealer_id,
    d.name, d.url, d.platform, d.scrape_method, d.active,
    d.last_scan_at, d.last_scan_status,
    COUNT(*) FILTER (WHERE i.status = 'active')                                               AS in_stock,
    COUNT(*) FILTER (WHERE i.status = 'sold')                                                  AS sold_total,
    COUNT(*) FILTER (WHERE i.status = 'active'
                       AND COALESCE(i.source_added_at, i.first_seen_at) >  NOW() - INTERVAL '30 days')  AS age_under_30,
    COUNT(*) FILTER (WHERE i.status = 'active'
                       AND COALESCE(i.source_added_at, i.first_seen_at) <= NOW() - INTERVAL '30 days'
                       AND COALESCE(i.source_added_at, i.first_seen_at) >  NOW() - INTERVAL '60 days')  AS age_30_60,
    COUNT(*) FILTER (WHERE i.status = 'active'
                       AND COALESCE(i.source_added_at, i.first_seen_at) <= NOW() - INTERVAL '60 days'
                       AND COALESCE(i.source_added_at, i.first_seen_at) >  NOW() - INTERVAL '90 days')  AS age_60_90,
    COUNT(*) FILTER (WHERE i.status = 'active'
                       AND COALESCE(i.source_added_at, i.first_seen_at) <= NOW() - INTERVAL '90 days')  AS age_over_90,
    COUNT(*) FILTER (WHERE i.status = 'sold'
                       AND i.sold_at      >  COALESCE(d.last_scan_at, NOW()) - INTERVAL '7 days')       AS sold_last_7d
FROM dealers d
LEFT JOIN dealer_inventory i ON i.dealer_id = d.id
GROUP BY d.id;

-- Grant EW app user the DML access it needs (superuser runs this block)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'expuser') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON dealers, dealer_inventory,
            dealer_inventory_history, dealer_scans, dealer_sold_signals TO expuser;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO expuser;
        GRANT SELECT ON dealer_stats TO expuser;
    END IF;
END
$$;
