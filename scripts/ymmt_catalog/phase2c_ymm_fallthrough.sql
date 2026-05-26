-- Phase 2c: for any catalog YMMT still uncovered, populate from same-YMM
-- (any trim) hits across all three sources. This is the 'fallthrough trim'
-- safety net — Bill will get a VIN, with trim_match='fallthrough' so he warns.

BEGIN;

-- From dealer_inventory: any YMM match
INSERT INTO ymmt_vin_cache (year, make, model, trim, vin, source, confidence, status)
SELECT DISTINCT ON (c.year, c.make, c.model, c.trim)
  c.year, c.make, c.model, c.trim,
  d.vin, 'db_inv_ymm', 'low', 'found'
FROM ymmt_catalog c
LEFT JOIN ymmt_vin_cache existing
  ON existing.year = c.year
 AND UPPER(existing.make) = UPPER(c.make)
 AND UPPER(existing.model) = UPPER(c.model)
 AND existing.trim = c.trim
JOIN dealer_inventory d
  ON d.year = c.year
 AND UPPER(d.make)  = UPPER(c.make)
 AND UPPER(d.model) = UPPER(c.model)
 AND d.status = 'active'
 AND d.vin IS NOT NULL
 AND length(d.vin) = 17
WHERE existing.vin IS NULL
ORDER BY c.year, c.make, c.model, c.trim, d.last_seen_at DESC
ON CONFLICT (year, make, model, trim) DO NOTHING;

-- From bids: any YMM match
INSERT INTO ymmt_vin_cache (year, make, model, trim, vin, source, confidence, status)
SELECT DISTINCT ON (c.year, c.make, c.model, c.trim)
  c.year, c.make, c.model, c.trim,
  b.vin, 'db_bids_ymm', 'low', 'found'
FROM ymmt_catalog c
LEFT JOIN ymmt_vin_cache existing
  ON existing.year = c.year
 AND UPPER(existing.make) = UPPER(c.make)
 AND UPPER(existing.model) = UPPER(c.model)
 AND existing.trim = c.trim
JOIN bids b
  ON b.year = c.year
 AND UPPER(b.make)  = UPPER(c.make)
 AND UPPER(b.model) = UPPER(c.model)
 AND b.vin IS NOT NULL
 AND length(b.vin) = 17
WHERE existing.vin IS NULL
ORDER BY c.year, c.make, c.model, c.trim, b.created_at DESC
ON CONFLICT (year, make, model, trim) DO NOTHING;

COMMIT;

SELECT
  (SELECT count(*) FROM ymmt_catalog) AS catalog_total,
  (SELECT count(*) FROM ymmt_vin_cache WHERE status='found') AS cache_found,
  ROUND(100.0 * (SELECT count(*) FROM ymmt_vin_cache WHERE status='found')
         / NULLIF((SELECT count(*) FROM ymmt_catalog), 0), 1) AS pct_covered;
