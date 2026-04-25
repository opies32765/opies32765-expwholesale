-- AI-discovered dealer scraping configs.
-- When a new dealer's platform doesn't match any known fingerprint, the
-- discover_dealer.py worker spawns Opus and writes a JSON recipe here.
ALTER TABLE dealers ADD COLUMN IF NOT EXISTS scrape_config JSONB;
ALTER TABLE dealers ADD COLUMN IF NOT EXISTS scrape_config_version INTEGER DEFAULT 0;
ALTER TABLE dealers ADD COLUMN IF NOT EXISTS scrape_config_at TIMESTAMPTZ;

-- Track AI discovery cost / runs for transparency.
CREATE TABLE IF NOT EXISTS dealer_discovery_runs (
    id BIGSERIAL PRIMARY KEY,
    dealer_id INTEGER NOT NULL REFERENCES dealers(id) ON DELETE CASCADE,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd NUMERIC(10,4),
    status TEXT,           -- 'ok' | 'failed' | 'manual_review'
    error TEXT,
    config_produced JSONB
);
CREATE INDEX IF NOT EXISTS idx_dealer_discovery_runs_dealer_id
    ON dealer_discovery_runs(dealer_id, started_at DESC);
