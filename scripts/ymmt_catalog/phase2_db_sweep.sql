-- Phase 2: populate ymmt_vin_cache from existing DBs
-- Sources: bids, dealer_inventory, LSL deals (via FDW or staging)
-- Strategy: for each ymmt_catalog row, find the closest VIN from each source,
-- prefer dealer_inventory (live VINs) > bids (we appraised it) > LSL deals (historical).

BEGIN;

-- 1. dealer_inventory hits (highest confidence: live VINs on partner lots)
INSERT INTO ymmt_vin_cache (year, make, model, trim, vin, source, confidence, status)
SELECT DISTINCT ON (c.year, c.make, c.model, c.trim)
  c.year, c.make, c.model, c.trim,
  d.vin, 'db_inv', 'high', 'found'
FROM ymmt_catalog c
JOIN dealer_inventory d
  ON d.year = c.year
 AND UPPER(d.make)  = UPPER(c.make)
 AND UPPER(d.model) = UPPER(c.model)
 AND LOWER(COALESCE(d.trim, '')) LIKE '%' || LOWER(c.trim) || '%'
 AND d.status = 'active'
 AND d.vin IS NOT NULL
 AND length(d.vin) = 17
ORDER BY c.year, c.make, c.model, c.trim, d.last_seen_at DESC
ON CONFLICT (year, make, model, trim) DO NOTHING;

-- 2. bids hits (we've appraised this YMMT)
INSERT INTO ymmt_vin_cache (year, make, model, trim, vin, source, confidence, status)
SELECT DISTINCT ON (c.year, c.make, c.model, c.trim)
  c.year, c.make, c.model, c.trim,
  b.vin, 'db_bids', 'high', 'found'
FROM ymmt_catalog c
JOIN bids b
  ON b.year = c.year
 AND UPPER(b.make)  = UPPER(c.make)
 AND UPPER(b.model) = UPPER(c.model)
 AND LOWER(COALESCE(b.trim, '')) LIKE '%' || LOWER(c.trim) || '%'
 AND b.vin IS NOT NULL
 AND length(b.vin) = 17
ORDER BY c.year, c.make, c.model, c.trim, b.created_at DESC
ON CONFLICT (year, make, model, trim) DO NOTHING;

-- 3. (LSL deals are in SQLite; populated by separate Python script)

COMMIT;

-- Report coverage
SELECT
  (SELECT count(*) FROM ymmt_catalog) AS catalog_total,
  (SELECT count(*) FROM ymmt_vin_cache WHERE status='found') AS cache_found,
  ROUND(100.0 * (SELECT count(*) FROM ymmt_vin_cache WHERE status='found')
         / NULLIF((SELECT count(*) FROM ymmt_catalog), 0), 1) AS pct_covered;
