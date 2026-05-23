-- ============================================================
-- 2026-05-22 — Buy Profile v2: per-model rollup + active_n
-- DRAFT — not deployed. Review before running.
--
-- Strictly additive: existing JSONB keys remain unchanged.
-- New keys under each make:
--   active_n   INT     — count of currently-on-lot units of this make
--   models     JSONB   — { "<MODEL>": { active_n, sold_n, avg_price,
--                                       avg_days_on_lot, last_seen } }
-- Profile version bumped 1 -> 2.
--
-- Deploy steps:
--   1) Apply this migration on C1 only (PG 5433 / expwholesale)
--   2) Either wait for 08:30 EDT cron, or:
--        SELECT rebuild_all_buy_profiles();
--   3) Spot-check txtcharlie: jsonb_path_query(buy_profile,
--        '$.makes.LAMBORGHINI.models')
-- Rollback: re-apply 2026-05-12_buy_profile.sql to restore old function.
-- ============================================================

CREATE OR REPLACE FUNCTION build_dealer_buy_profile(p_dealer_id INT)
RETURNS JSONB LANGUAGE plpgsql AS $$
DECLARE
  v_sample  JSONB;
  v_bands   JSONB;
  v_makes   JSONB;
  v_profile JSONB;
BEGIN
  -- sample (unchanged)
  SELECT jsonb_build_object(
    'active_n',      COUNT(*) FILTER (WHERE status='active'),
    'history_n',     COUNT(*),
    'sold_n_180d',   COUNT(*) FILTER (WHERE status!='active'
                                     AND sold_at >= NOW() - INTERVAL '180 days'),
    'first_scan_at', MIN(first_seen_at)::date,
    'days_scanned',  GREATEST(EXTRACT(DAY FROM NOW() - MIN(first_seen_at))::int, 0)
  ) INTO v_sample
  FROM dealer_inventory WHERE dealer_id = p_dealer_id;

  -- bands (unchanged)
  SELECT jsonb_build_object(
    'year_p10',  percentile_disc(0.10) WITHIN GROUP (ORDER BY year),
    'year_p90',  percentile_disc(0.90) WITHIN GROUP (ORDER BY year),
    'year_min',  MIN(year), 'year_max', MAX(year),
    'price_p10', percentile_disc(0.10) WITHIN GROUP (ORDER BY price),
    'price_p90', percentile_disc(0.90) WITHIN GROUP (ORDER BY price),
    'miles_p10', percentile_disc(0.10) WITHIN GROUP (ORDER BY mileage),
    'miles_p90', percentile_disc(0.90) WITHIN GROUP (ORDER BY mileage)
  ) INTO v_bands
  FROM dealer_inventory
  WHERE dealer_id = p_dealer_id AND status='active' AND price > 0;

  -- makes rollup — NEW: emits active_n + nested models map
  WITH weighted AS (
    SELECT UPPER(make)  AS make,
           UPPER(model) AS model,
           year, price, mileage, status, sold_at, first_seen_at,
           EXP(-EXTRACT(EPOCH FROM (NOW() - first_seen_at)) / (86400 * 90.0)) AS w
      FROM dealer_inventory
     WHERE dealer_id = p_dealer_id AND make IS NOT NULL
  ),
  model_agg AS (
    SELECT make, model,
           COUNT(*) FILTER (WHERE status='active') AS active_n,
           COUNT(*) FILTER (WHERE status!='active' AND sold_at IS NOT NULL) AS sold_n,
           ROUND(AVG(price) FILTER (WHERE status='active'))::int AS avg_price,
           ROUND(AVG(EXTRACT(EPOCH FROM (sold_at - first_seen_at))/86400)
                 FILTER (WHERE status!='active' AND sold_at IS NOT NULL))::int AS avg_days,
           MAX(first_seen_at)::date AS last_seen
      FROM weighted
     WHERE model IS NOT NULL AND model <> ''
     GROUP BY make, model
  ),
  model_obj AS (
    SELECT make,
           jsonb_object_agg(model, jsonb_build_object(
             'active_n',        active_n,
             'sold_n',          sold_n,
             'avg_price',       avg_price,
             'avg_days_on_lot', avg_days,
             'last_seen',       last_seen
           )) AS models
      FROM model_agg
     GROUP BY make
  ),
  agg AS (
    SELECT w.make,
           SUM(w.w) AS w_n,
           COUNT(*) FILTER (WHERE status='active')                          AS active_n,
           ROUND(AVG(year)    FILTER (WHERE status='active'))::int          AS avg_year,
           ROUND(AVG(price)   FILTER (WHERE status='active'))::int          AS avg_price,
           ROUND(AVG(mileage) FILTER (WHERE status='active'))::int          AS avg_miles,
           COUNT(*) FILTER (WHERE status!='active' AND sold_at IS NOT NULL) AS sold_n,
           ROUND(AVG(EXTRACT(EPOCH FROM (sold_at - first_seen_at))/86400)
                 FILTER (WHERE status!='active' AND sold_at IS NOT NULL))::int AS avg_days,
           MAX(first_seen_at)::date                                         AS last_seen
      FROM weighted w GROUP BY w.make
  ),
  tot AS (SELECT SUM(w_n) AS t FROM agg)
  SELECT jsonb_object_agg(agg.make, jsonb_build_object(
           'share',           ROUND((agg.w_n / NULLIF(tot.t,0) * 100.0)::numeric, 1),
           'active_n',        agg.active_n,                              -- NEW
           'avg_year',        agg.avg_year,
           'avg_price',       agg.avg_price,
           'avg_miles',       agg.avg_miles,
           'sold_n',          agg.sold_n,
           'avg_days_on_lot', agg.avg_days,
           'last_seen',       agg.last_seen,
           'models',          COALESCE(mo.models, '{}'::jsonb)            -- NEW
         )) INTO v_makes
    FROM agg
         LEFT JOIN model_obj mo ON mo.make = agg.make,
         tot;

  -- profile envelope (version bumped to 2)
  SELECT jsonb_build_object(
    'version',    2,
    'built_at',   NOW(),
    'sample',     COALESCE(v_sample, '{}'::jsonb),
    'bands',      COALESCE(v_bands,  '{}'::jsonb),
    'makes',      COALESCE(v_makes,  '{}'::jsonb),
    'behavioral', COALESCE(buy_profile->'behavioral', '{}'::jsonb),
    'overrides',  COALESCE(buy_profile->'overrides',  '{}'::jsonb)
  ) INTO v_profile
  FROM dealers WHERE id = p_dealer_id;

  UPDATE dealers
     SET buy_profile          = v_profile,
         buy_profile_built_at = NOW()
   WHERE id = p_dealer_id;

  RETURN v_profile;
END $$;

ALTER FUNCTION build_dealer_buy_profile(INT) OWNER TO expuser;

-- rebuild_all_buy_profiles() and record_push_outcome() are unchanged.
