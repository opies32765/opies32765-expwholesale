-- ============================================================
-- 2026-05-26 — Persistent screenshot cache
--
-- Bid screenshots (iPacket sticker, AccuTrade report, Carfax, AutoCheck)
-- are stored as local PNG files in /ipacket_reports/, /accutrade_reports/,
-- /vauto_reports/. Lookup rows in ipacket_lookups/accutrade_lookups/
-- vauto_lookups reference the file paths.
--
-- Problem: force-reprocess DELETEs those lookup rows. During the 60-90s
-- window before workers re-scrape, the bid card has no path to render.
-- Operator sees "snapshot broken/missing" intermittently.
--
-- Fix: this cache table is written EVERY time a worker successfully
-- captures a screenshot, regardless of whether the lookup row exists.
-- Survives force-reprocess. Bid card uses the most recent cached path
-- per (bid_id, source) when the live lookup row's screenshot is null.
-- ============================================================

CREATE TABLE IF NOT EXISTS bid_screenshot_cache (
  id            SERIAL PRIMARY KEY,
  bid_id        INTEGER NOT NULL REFERENCES bids(id) ON DELETE CASCADE,
  source        TEXT NOT NULL,    -- 'ipacket' | 'accutrade' | 'carfax' | 'autocheck'
  screenshot    TEXT NOT NULL,    -- /ipacket_reports/... etc.
  captured_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (source IN ('ipacket','accutrade','carfax','autocheck'))
);

CREATE INDEX IF NOT EXISTS idx_bid_screenshot_cache_lookup
  ON bid_screenshot_cache (bid_id, source, captured_at DESC);

ALTER TABLE bid_screenshot_cache OWNER TO expuser;
GRANT SELECT, INSERT ON bid_screenshot_cache TO expuser;
GRANT USAGE, SELECT ON SEQUENCE bid_screenshot_cache_id_seq TO expuser;

-- ============================================================
-- Backfill from current lookup rows so existing bids have a cache entry.
-- One-time. Worker hooks will keep it populated going forward.
-- ============================================================

INSERT INTO bid_screenshot_cache (bid_id, source, screenshot, captured_at)
SELECT bid_id, 'ipacket', screenshot, COALESCE(looked_up_at, NOW())
  FROM ipacket_lookups
 WHERE screenshot IS NOT NULL AND screenshot <> ''
ON CONFLICT DO NOTHING;

INSERT INTO bid_screenshot_cache (bid_id, source, screenshot, captured_at)
SELECT bid_id, 'accutrade', screenshot, COALESCE(looked_up_at, NOW())
  FROM accutrade_lookups
 WHERE screenshot IS NOT NULL AND screenshot <> ''
ON CONFLICT DO NOTHING;

INSERT INTO bid_screenshot_cache (bid_id, source, screenshot, captured_at)
SELECT bid_id, 'carfax', carfax_screenshot, COALESCE(looked_up_at, NOW())
  FROM vauto_lookups
 WHERE carfax_screenshot IS NOT NULL AND carfax_screenshot <> ''
ON CONFLICT DO NOTHING;

INSERT INTO bid_screenshot_cache (bid_id, source, screenshot, captured_at)
SELECT bid_id, 'autocheck', autocheck_screenshot, COALESCE(looked_up_at, NOW())
  FROM vauto_lookups
 WHERE autocheck_screenshot IS NOT NULL AND autocheck_screenshot <> ''
ON CONFLICT DO NOTHING;

-- Sanity check
SELECT source, COUNT(*) AS n, MIN(captured_at)::date AS oldest, MAX(captured_at)::date AS newest
  FROM bid_screenshot_cache GROUP BY source ORDER BY source;
