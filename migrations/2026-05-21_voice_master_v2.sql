-- v2: add rich vAuto/Manheim/dealer_inv aggregations to voice_ymm_master.
-- All data sourced from existing postgres tables (vauto_lookups,
-- dealer_inventory) — no new external API calls needed.

ALTER TABLE voice_ymm_master
  -- vAuto rBook pool (aggregated across all our last-90d bids for this YMM)
  ADD COLUMN IF NOT EXISTS rbook_pool_count        INTEGER,
  ADD COLUMN IF NOT EXISTS rbook_top_comps         JSONB,    -- top 20 closest-mile, dedup by VIN
  ADD COLUMN IF NOT EXISTS rbook_median_dol        INTEGER,
  ADD COLUMN IF NOT EXISTS rbook_certified_pct     NUMERIC,
  ADD COLUMN IF NOT EXISTS rbook_pending_pct       NUMERIC,
  ADD COLUMN IF NOT EXISTS rbook_carfax_clean_pct  NUMERIC,

  -- Manheim transaction pool (real wholesale auction sales)
  ADD COLUMN IF NOT EXISTS manheim_pool_count      INTEGER,
  ADD COLUMN IF NOT EXISTS manheim_recent_sales    JSONB,    -- last 10 with price+miles+date+grade
  ADD COLUMN IF NOT EXISTS manheim_median_sale     INTEGER,
  ADD COLUMN IF NOT EXISTS manheim_avg_condition   NUMERIC,
  ADD COLUMN IF NOT EXISTS manheim_last_sale_at    DATE,

  -- Dealer inventory FULL snapshot (active + recently sold)
  ADD COLUMN IF NOT EXISTS dealer_inv_active_full  JSONB,    -- all matching active rows (not just top 3)
  ADD COLUMN IF NOT EXISTS dealer_inv_sold_full    JSONB,    -- all matching sold rows last 30d
  ADD COLUMN IF NOT EXISTS dealer_inv_price_drops  JSONB,    -- active rows with price_drop_amount > 0

  -- Days on lot stats from dealer inventory
  ADD COLUMN IF NOT EXISTS dealer_active_min_dol   INTEGER,
  ADD COLUMN IF NOT EXISTS dealer_active_max_dol   INTEGER,
  ADD COLUMN IF NOT EXISTS dealer_active_p50_dol   INTEGER,
  ADD COLUMN IF NOT EXISTS dealer_sold_avg_dol     INTEGER;
