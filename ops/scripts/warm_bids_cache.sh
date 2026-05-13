#!/bin/bash
# Pre-warm PG buffer cache with the most recent 100 bids and their related rows.
# Runs every few minutes via cron. Negligible load — touches ~100 bid IDs across
# 4 tables. Keeps all hot data resident so clients never hit cold-cache.
export PGPASSWORD='ExpWholesale2026!'
psql -U expuser -h localhost -p 5433 -d expwholesale -tAc "
WITH recent AS (
  SELECT id FROM bids ORDER BY id DESC LIMIT 100
)
SELECT
  (SELECT COUNT(*) FROM bids b WHERE b.id IN (SELECT id FROM recent)) +
  (SELECT COUNT(*) FROM bid_photos bp WHERE bp.bid_id IN (SELECT id FROM recent)) +
  (SELECT COUNT(*) FROM vauto_lookups vl WHERE vl.bid_id IN (SELECT id FROM recent)) +
  (SELECT COUNT(*) FROM accutrade_lookups al WHERE al.bid_id IN (SELECT id FROM recent)) +
  (SELECT COUNT(*) FROM ipacket_lookups il WHERE il.bid_id IN (SELECT id FROM recent)) +
  (SELECT COUNT(*) FROM contacts c WHERE c.id IN (SELECT contact_id FROM bids WHERE id IN (SELECT id FROM recent))) +
  (SELECT COUNT(*) FROM dealers d WHERE d.id IN (SELECT partner_dealer_id FROM bids WHERE id IN (SELECT id FROM recent)))
" >/dev/null
