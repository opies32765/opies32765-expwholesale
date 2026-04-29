-- Shared vAuto auth state. ONE row, holds the cookie jar + dealer-scoped
-- headers. Refreshed by the cookie_keeper running alongside a long-lived
-- Chrome session keeper. API workers read on every call.

BEGIN;

CREATE TABLE IF NOT EXISTS vauto_session (
    id              SERIAL PRIMARY KEY,
    label           TEXT UNIQUE NOT NULL,            -- e.g. 'oscarpas'
    cookies         JSONB NOT NULL,                  -- {name: value} flat dict
    entity_id       TEXT NOT NULL,                   -- appraisal/currententityid
    platform_user_id TEXT NOT NULL,
    user_agent      TEXT,
    refreshed_at    TIMESTAMPTZ DEFAULT NOW(),
    refreshed_by    TEXT,                            -- 'cookie_keeper' | 'manual'
    notes           TEXT
);

-- Track per-bid API enrichment so we don't re-fetch on every poll.
ALTER TABLE vauto_lookups
  ADD COLUMN IF NOT EXISTS api_refreshed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS api_carfax       JSONB,
  ADD COLUMN IF NOT EXISTS api_price_guides JSONB;

-- Idle-poll index: workers grab rows where api_refreshed_at IS NULL OR is older
-- than 6 hours (book values can drift mid-day on hot vehicles).
CREATE INDEX IF NOT EXISTS idx_vauto_lookups_api_refresh
  ON vauto_lookups (api_refreshed_at NULLS FIRST);

COMMIT;
