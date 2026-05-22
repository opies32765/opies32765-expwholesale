-- voice_ymm_master — pre-built per-YMM intelligence table.
-- Seed source: any YMM we've purchased in the last 90 days via LSL.
-- Refreshed nightly by build_voice_master.py.
-- Voice bot checks this FIRST in the quick-path; on cache hit, skips
-- vAuto BFF round trip entirely.

CREATE TABLE IF NOT EXISTS voice_ymm_master (
    id              BIGSERIAL PRIMARY KEY,
    -- Identity (composite unique key)
    year            INTEGER NOT NULL,
    make            TEXT    NOT NULL,
    model           TEXT    NOT NULL,
    miles_band      INTEGER NOT NULL,        -- bucketed to nearest 5K (5K, 10K, 15K, ...)

    -- Where this entry came from
    seed_source     TEXT,                    -- 'lsl_90d' | 'frequent_bid' | 'manual_pin'
    seed_rank       INTEGER,                 -- ranking by purchase frequency

    -- LSL purchase history (90-day rolling window)
    lsl_count_90d           INTEGER,
    lsl_avg_purchase_cost   NUMERIC,
    lsl_median_purchase     NUMERIC,
    lsl_avg_sale_price      NUMERIC,
    lsl_avg_front           NUMERIC,
    lsl_last_purchase_date  DATE,
    lsl_recent_deals_sample JSONB,

    -- Partner dealer inventory (live + recently-sold)
    partner_active_count        INTEGER,
    partner_active_avg_price    NUMERIC,
    partner_active_avg_miles    INTEGER,
    partner_active_avg_dol      NUMERIC,     -- days on lot, active
    partner_active_sample       JSONB,       -- top 3: dealer/price/miles/DOL
    partner_sold_30d_count      INTEGER,
    partner_sold_30d_sample     JSONB,
    partner_with_price_drop     INTEGER,     -- # active with price_drop_amount > 0
    partner_total_drop_amount   NUMERIC,

    -- vAuto live data (refreshed via BFF)
    mmr_wholesale_avg       INTEGER,
    mmr_retail_avg          INTEGER,
    mmr_sample_n            INTEGER,
    mmr_avg_odometer        INTEGER,
    rbook_median_retail     INTEGER,
    rbook_p25               INTEGER,
    rbook_p75               INTEGER,
    rbook_listing_count     INTEGER,
    rbook_closest_anchor    JSONB,           -- closest-mile rBook listing
    vauto_refreshed_at      TIMESTAMPTZ,

    -- Our prior bid history (last 90d)
    prior_bid_count_90d     INTEGER,
    prior_bid_avg_mmr       INTEGER,
    prior_bid_avg_rbook     INTEGER,
    prior_bid_avg_our_offer NUMERIC,
    prior_bids_sample       JSONB,

    -- Pre-baked Sonnet response (optional, refreshed nightly)
    sonnet_narrative        TEXT,
    tts_audio_b64           TEXT,
    narrative_generated_at  TIMESTAMPTZ,

    -- Lifecycle
    refreshed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT (NOW() + INTERVAL '36 hours'),

    UNIQUE (year, make, model, miles_band)
);

CREATE INDEX IF NOT EXISTS idx_voice_ymm_lookup
    ON voice_ymm_master (year, UPPER(make), UPPER(model), miles_band)
    WHERE expires_at > NOW();

CREATE INDEX IF NOT EXISTS idx_voice_ymm_refreshed
    ON voice_ymm_master (refreshed_at);

COMMENT ON TABLE voice_ymm_master IS
    'Pre-built per-YMM intelligence for the voice bot. Seeded from LSL '
    'last-90d purchases; refreshed nightly. Cache hit → bot skips vAuto BFF.';
