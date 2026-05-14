-- 2026-05-14: trim_match_cache — per-pair cached Sonnet decision on
-- whether two trim descriptors refer to the same factory variant for a
-- given (make, model). Used by claude_trim_match.is_same_trim and
-- dealer_match.find_dealer_matches Tier-1.5 filter. Key is sorted-pair
-- so equivalence lookups are symmetric.
CREATE TABLE IF NOT EXISTS trim_match_cache (
    id           SERIAL PRIMARY KEY,
    make         TEXT NOT NULL,
    model        TEXT NOT NULL,
    trim_a       TEXT NOT NULL DEFAULT '',
    trim_b       TEXT NOT NULL DEFAULT '',
    is_match     BOOLEAN NOT NULL,
    confidence   NUMERIC(3,2),
    reason       TEXT,
    source       TEXT NOT NULL DEFAULT 'claude_sonnet_4_6',
    decided_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS trim_match_cache_key
    ON trim_match_cache (make, model, trim_a, trim_b);
