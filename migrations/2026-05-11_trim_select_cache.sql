-- Trim selection cache for AccuTrade overseer
-- One row per VIN: which trim the overseer chose, so we never re-ask.

CREATE TABLE IF NOT EXISTS accutrade_trim_select_cache (
    vin TEXT PRIMARY KEY,
    choices_json JSONB NOT NULL,
    selected_index INTEGER NOT NULL,
    selected_text TEXT NOT NULL,
    confidence NUMERIC(4,3),
    model_used TEXT,
    bid_id_first_seen INTEGER,
    bid_trim_at_select TEXT,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trim_select_created ON accutrade_trim_select_cache(created_at DESC);

ALTER TABLE accutrade_trim_select_cache OWNER TO expuser;

-- Also add selected_trim_text to accutrade_lookups for auditing what was actually clicked
ALTER TABLE accutrade_lookups ADD COLUMN IF NOT EXISTS selected_trim_text TEXT;
ALTER TABLE accutrade_lookups ADD COLUMN IF NOT EXISTS trim_select_source TEXT;
