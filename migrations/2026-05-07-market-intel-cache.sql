-- 2026-05-07 — bid_detail page speed fix
-- Add a cached column so /bid/<id> doesn't have to read the heavy
-- rbook_competitive_set + manheim_transactions JSONB columns on every page
-- render. ~152 KB of TOAST decompression saved per page load on popular bids.
--
-- Populated by:
--   - vauto_enrichment.kick_direct_enrichment (writes on every direct API run)
--   - app.py bid_detail (lazy-fills on first view of bids predating the cache)

ALTER TABLE vauto_lookups
  ADD COLUMN IF NOT EXISTS market_intel_cached JSONB;

-- Backfill recent 200 bids (production already done). To re-backfill, run:
--   /opt/expwholesale/venv/bin/python -c "..." (see commit notes)
